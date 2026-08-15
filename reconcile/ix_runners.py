"""Reconcile the self-hosted ix runner pool to this repo's runner config.

Runs from CI on GITHUB-HOSTED runners only (never self-hosted: a runner VM
must never see IX_TOKEN). Secrets: IX_TOKEN (the ix account the VMs bill
to), RUNNER_PAT (fine-grained, "administration" repo rw). The PAT never
leaves that runner: it mints SHORT-LIVED (1 h) registration tokens and
reads runner status; a VM only ever receives a registration token, which
can do nothing but register a runner and is dead within the hour.

Provisioning is pure ix Python SDK (ix-sdk on PyPI, pinned in the
entrypoint's inline metadata) - no CLI, no output parsing. The
registration token rides ``secrets().set()`` plus a ``secret_files``
attach at CREATE time, so it is present at first boot as a root-only file
and no post-boot seeding step exists; unbuilt templates compile
server-side on first boot.

Per pool member:
    missing VM                      -> create
    VM on a stale runner-config rev -> replace (DEFERRED while any of its
                                       runners is busy: a config roll must
                                       not kill running jobs)
    runners offline, VM reachable   -> repair (restart units);
                                       still offline on the NEXT run -> replace
    VM unreachable                  -> replace

One member's failure never aborts the run: it is logged, the budget is
spent, and the loop moves on - the pool converges across runs.

MAX_REPLACEMENTS caps creations+replacements PER RUN: a bad template rev
stalls loudly after N boots instead of thrashing the pool forever, and a
config roll converges across successive runs like a rolling update.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import urllib.error
import urllib.request

# Paths whose last-touching commit defines the desired runner-config rev.
CONFIG_PATHS = ["nix/", "flake.nix", "flake.lock"]
# Baked into the image by the flake (specialArgs self.rev ->
# /etc/ix-runner/rev); read back here for the staleness check.
REV_PATH = "/etc/ix-runner/rev"
# Two-strike marker, recorded on the VM itself so this script stays
# stateless across runs.
REPAIRED_MARKER = "/var/lib/ix-runner/repaired"
# A wedged VM must not hang the reconcile.
EXEC_TIMEOUT = 60
# Bounds a create: a first boot of a new rev builds the template in-guest.
CREATE_TIMEOUT = 1800


def log_error(message: str) -> None:
    """An Actions error annotation - surfaced on the run, not buried in logs."""
    print(f"::error::{message}")


def log_warning(message: str) -> None:
    """An Actions warning annotation."""
    print(f"::warning::{message}")


def client():
    """The ix API client; resolves IX_TOKEN from the environment."""
    from ix_sdk import Client

    return Client()


def github_api(pat: str, repo: str, path: str, *, method: str = "GET") -> dict:
    """Call the GitHub REST API with the PAT; return the parsed JSON body."""
    api = os.environ.get("GITHUB_API_URL", "https://api.github.com")
    request = urllib.request.Request(
        f"{api}/repos/{repo}{path}",
        method=method,
        headers={"Authorization": f"Bearer {pat}"},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            body = response.read()
            return json.loads(body) if body else {}
    except urllib.error.HTTPError as error:
        # Fine-grained PATs expire, and the whole reconcile is dead until one
        # is minted again; say that, rather than leaving a bare 401 in a log.
        if error.code == 401:
            log_error(
                "RUNNER_PAT was rejected (HTTP 401): it has EXPIRED or been"
                " revoked. Mint a new fine-grained PAT with Administration"
                " read/write on this repo and update the RUNNER_PAT secret."
            )
            raise SystemExit(1) from error
        raise


def list_runners(pat: str, repo: str) -> list[dict]:
    """Every self-hosted runner registered on the repo, across ALL pages.

    A short read is not merely incomplete, it is destructive: a member whose
    runners fall off the end of page one reads offline and gets replaced, so
    an unpaginated listing mass-replaces the pool the moment it passes 100
    registrations (POOL_SIZE x slots).
    """
    runners: list[dict] = []
    total = 0
    page = 1
    while True:
        body = github_api(pat, repo, f"/actions/runners?per_page=100&page={page}")
        total = int(body.get("total_count") or 0)
        batch = body.get("runners") or []
        runners.extend(batch)
        if not batch or len(runners) >= total:
            break
        page += 1
    if len(runners) < total:
        log_error(
            f"runner listing is short: {len(runners)} of {total} runners."
            " Refusing to reconcile - every unlisted member would read"
            " offline and be replaced."
        )
        raise SystemExit(1)
    return runners


def git(*args: str) -> str:
    """Run git in the checkout; return its stdout, stripped."""
    result = subprocess.run(["git", *args], capture_output=True, text=True, check=True)
    return result.stdout.strip()


def desired_rev() -> str:
    """Last commit touching the runner config, NOT GITHUB_SHA.

    Unrelated merges must not roll the fleet, and the template cache is
    keyed by exact rev (never provision from a branch name: it re-resolves).
    """
    # A shallow checkout's grafted boundary commit diffs against the empty
    # tree, so `git log -- <paths>` names HEAD for EVERY commit and the whole
    # fleet rolls on every push - silently, because the rev looks plausible.
    if git("rev-parse", "--is-shallow-repository") == "true":
        log_error(
            "the checkout is shallow, so the runner-config rev cannot be"
            " resolved (a grafted history makes every commit look like a"
            " config change and rolls the whole pool). Set `fetch-depth: 0`"
            " on actions/checkout."
        )
        raise SystemExit(1)
    rev = git("log", "-1", "--format=%H", "--", *CONFIG_PATHS)
    if not rev:
        log_error(
            "could not resolve the runner-config rev: no commit in this"
            f" history touches {' '.join(CONFIG_PATHS)}"
        )
        raise SystemExit(1)
    return rev


def pool_name() -> str:
    """The pool's name: POOL_NAME env, or the repository's name.

    Everything derives from it, matching the nix module's poolName option:
    VM names `<pool>-runner-<N>`, runner names `<pool>-r<N>-<slot>`, and the
    secret store key `<pool>_runner_reg_token`.
    """
    explicit = os.environ.get("POOL_NAME")
    if explicit:
        return explicit
    return os.environ["GITHUB_REPOSITORY"].split("/")[1].lower()


def member_online(runners: list[dict], pool: str, member: int) -> bool:
    """Any runner daemon of pool member N online?"""
    prefix = f"{pool}-r{member}-"
    return any(
        runner["name"].startswith(prefix) and runner["status"] == "online"
        for runner in runners
    )


def deregister_member(
    pat: str, repo: str, runners: list[dict], pool: str, member: int
) -> bool:
    """Delete pool member N's runner registrations; False when one is busy.

    GitHub refuses to delete a busy runner's registration (HTTP 422), which
    makes this the atomic guard against rolling a VM out from under a job:
    a busy check alone races the scan's snapshot. Deregistering first makes
    GitHub itself the lock - only a member with zero registrations left is
    safe to delete.
    """
    prefix = f"{pool}-r{member}-"
    for runner in runners:
        if not runner["name"].startswith(prefix):
            continue
        try:
            github_api(pat, repo, f"/actions/runners/{runner['id']}", method="DELETE")
        except urllib.error.HTTPError as error:
            if error.code == 422:  # busy: picked up a job since the scan
                return False
            if error.code == 404:  # already gone
                continue
            raise
    return True


def member_busy(runners: list[dict], pool: str, member: int) -> bool:
    """Any runner daemon of pool member N mid-job?"""
    prefix = f"{pool}-r{member}-"
    return any(
        runner["name"].startswith(prefix) and runner.get("busy") for runner in runners
    )


async def member_rev(machine) -> str | None:
    """The config rev a member's image was built from; None if unreachable."""
    try:
        content = await asyncio.wait_for(machine.read_file(REV_PATH), EXEC_TIMEOUT)
        return content.strip()
    # IxError subclasses RuntimeError; TimeoutError covers the wait_for bound.
    except (TimeoutError, OSError, RuntimeError):
        return None


async def guest(machine, *command: str) -> bool:
    """Run a command in the guest; True when it exited 0, False otherwise."""
    try:
        result = await asyncio.wait_for(machine.exec(list(command)), EXEC_TIMEOUT)
        return result.exit_code == 0
    # IxError subclasses RuntimeError; TimeoutError covers the wait_for bound.
    except (TimeoutError, OSError, RuntimeError):
        return False


def create_options(**kwargs):
    """Construct CreateMachineOptions; a seam so tests never import the SDK
    (the wheel is x86_64-only; the fakes stand in for it anyway)."""
    from ix_sdk import CreateMachineOptions

    return CreateMachineOptions(**kwargs)


async def create(ix, repo: str, rev: str, secret: str, member: int, name: str) -> None:
    """Provision one pool member; the registration token is already stored.

    The token reaches the VM as a root-only file via the secret_files
    attach, present at first boot - no post-boot seeding step exists. An
    unbuilt (rev, attr) template compiles server-side on first boot
    (single-flight per pinned rev; idempotency_key is refused for flake-ref
    templates, so none is sent).
    """
    options = create_options(
        template=f"github:{repo}/{rev}#ci-runner-{member}",
        name=name,
        region=os.environ.get("IX_REGION") or "us-west-1",
        secret_files={secret: "runner-token"},
    )
    await asyncio.wait_for(ix.machines().create(options), CREATE_TIMEOUT)


async def reconcile(ix) -> int:
    """Converge the pool; return the number of creations/replacements.

    Three phases. The SCAN walks members in order and collects
    budget-admitted create/replace actions (cheap, sequential). One
    registration token is then minted and stored - GitHub registration
    tokens are repo-scoped and hour-valid, so one serves every member this
    run touches. The EXECUTE phase runs the actions concurrently under a
    bounded semaphore: the minutes in a roll are guest boots, and they
    overlap; a full-pool roll takes waves of CONCURRENCY instead of one
    boot at a time.
    """
    pat = os.environ["RUNNER_PAT"]
    repo = os.environ["GITHUB_REPOSITORY"]
    pool = pool_name()
    # POOL_SIZE x `slots` runner daemons each = the concurrent job budget
    # (the consuming flake's mkPool size and this must agree).
    pool_size = int(os.environ.get("POOL_SIZE") or 8)
    max_replacements = int(os.environ.get("MAX_REPLACEMENTS") or 2)
    concurrency = int(os.environ.get("CONCURRENCY") or 4)
    secret = os.environ.get("SECRET_NAME") or f"{pool}_runner_reg_token"

    rev = desired_rev()
    runners = list_runners(pat, repo)
    vms = {info.name: info.id for info in await ix.machines().list()}
    # Empty pool = first bootstrap: nothing exists to thrash, so the cap
    # protects nothing - raise it and build the whole pool in one run.
    empty = not any(f"{pool}-runner-{m}" in vms for m in range(1, pool_size + 1))
    if empty and max_replacements < pool_size:
        print(f"empty pool -> bootstrap: raising the cap to {pool_size}")
        max_replacements = pool_size

    replaced = 0
    failures = 0
    actions: list[tuple[str, int, str]] = []  # (kind, member, name)

    def admit(kind: str, member: int, name: str) -> bool:
        # Budget is spent at ADMISSION: a bad template rev stalls after N
        # attempts even when every attempt fails.
        nonlocal replaced
        if replaced >= max_replacements:
            print(
                f"replacement budget ({max_replacements}) exhausted;"
                " remaining members reconcile next run"
            )
            return False
        replaced += 1
        actions.append((kind, member, name))
        return True

    # -- scan --
    for member in range(1, pool_size + 1):
        name = f"{pool}-runner-{member}"
        if name not in vms:
            print(f"{name}: missing -> create")
            if not admit("create", member, name):
                break
            continue
        machine = ix.machines().connect(vms[name])
        actual = await member_rev(machine)
        if actual is None:
            print(f"{name}: unreachable -> replace")
            if not admit("replace", member, name):
                break
            continue
        if actual != rev:
            # Never roll a member out from under a running job: config
            # rolls wait for idleness, this member converges on a later run.
            if member_busy(runners, pool, member):
                print(f"{name}: stale rev but busy -> deferred")
                continue
            print(f"{name}: rev {actual[:12]} != {rev[:12]} -> replace")
            if not admit("replace", member, name):
                break
            continue
        if member_online(runners, pool, member):
            print(f"{name}: healthy")
            await guest(machine, "rm", "-f", REPAIRED_MARKER)
            continue
        # Offline but reachable and on the right rev: repair once by
        # restarting the units (a configured runner re-registers from its
        # persisted state and needs no fresh token); replace only if a prior
        # run already repaired and it is STILL offline (two-strike, with the
        # strike recorded on the VM itself so this script stays stateless).
        if await guest(machine, "test", "-f", REPAIRED_MARKER):
            print(f"{name}: still offline after repair -> replace")
            if not admit("replace", member, name):
                break
        else:
            print(f"{name}: runners offline -> repair (restart units)")
            await guest(machine, "systemctl", "restart", "github-runner-*")
            await guest(machine, "touch", REPAIRED_MARKER)

    # -- execute --
    if actions:
        token = github_api(
            pat, repo, "/actions/runners/registration-token", method="POST"
        )
        await ix.secrets().set(secret, token["token"])
        print(f"executing {len(actions)} action(s), concurrency {concurrency}")
        gate = asyncio.Semaphore(concurrency)

        async def run_action(kind: str, member: int, name: str) -> None:
            nonlocal replaced, failures
            async with gate:
                try:
                    if kind == "replace":
                        # Deregister at EXECUTE time, right before the
                        # delete: GitHub refuses (422) to deregister a busy
                        # runner, so a member that picked up a job since the
                        # scan is skipped, and its budget is refunded.
                        if not deregister_member(pat, repo, runners, pool, member):
                            print(f"{name}: picked up a job mid-scan -> deferred")
                            replaced -= 1
                            return
                        await ix.machines().connect(vms[name]).delete()
                    await create(ix, repo, rev, secret, member, name)
                except (TimeoutError, OSError, RuntimeError) as e:
                    failures += 1
                    print(f"{name}: {kind} FAILED ({e}); reconciling again next run")

        await asyncio.gather(*(run_action(*action) for action in actions))

    print(f"reconcile done: {replaced} creation(s)/replacement(s), {failures} failed")
    if failures:
        raise SystemExit(1)
    return replaced


def main() -> None:
    """Entry point: require the secrets, then converge."""
    for required in ("IX_TOKEN", "RUNNER_PAT", "GITHUB_REPOSITORY"):
        if not os.environ.get(required):
            raise SystemExit(f"{required} is required")
    asyncio.run(reconcile(client()))


if __name__ == "__main__":
    main()
