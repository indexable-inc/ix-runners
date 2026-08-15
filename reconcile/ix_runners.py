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
    with urllib.request.urlopen(request, timeout=30) as response:
        body = response.read()
        return json.loads(body) if body else {}


def desired_rev() -> str:
    """Last commit touching the runner config, NOT GITHUB_SHA.

    Unrelated merges must not roll the fleet, and the template cache is
    keyed by exact rev (never provision from a branch name: it re-resolves).
    """
    result = subprocess.run(
        ["git", "log", "-1", "--format=%H", "--", *CONFIG_PATHS],
        capture_output=True,
        text=True,
        check=True,
    )
    rev = result.stdout.strip()
    if not rev:
        raise SystemExit("could not resolve the runner-config rev (shallow checkout?)")
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

    GitHub refuses to delete a BUSY runner's registration (HTTP 422), which
    makes this the atomic guard against rolling a VM out from under a job:
    the busy check alone races, because a member can pick up a job between
    the scan's runners snapshot and the delete (observed live: two jobs
    died step-less when their member rolled seconds after passing the busy
    check). Deregistering first turns GitHub itself into the lock - only a
    member with zero registrations left is safe to delete.
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


async def create(
    ix, pat: str, repo: str, rev: str, pool: str, member: int, name: str
) -> None:
    """Provision one pool member with the registration token at first boot.

    A fresh 1 h registration token goes into the ix secret store (an API
    body, never argv) and reaches the VM as a root-only file via the
    secret_files attach; nothing durable ever lands on a VM. The runner
    units read it as root at configure time and skip cleanly while it is
    absent. An unbuilt (rev, attr) template compiles server-side on first
    boot (single-flight per pinned rev; idempotency_key is refused for
    flake-ref templates, so none is sent).
    """
    secret = os.environ.get("SECRET_NAME") or f"{pool}_runner_reg_token"
    token = github_api(pat, repo, "/actions/runners/registration-token", method="POST")
    await ix.secrets().set(secret, token["token"])
    options = create_options(
        template=f"github:{repo}/{rev}#ci-runner-{member}",
        name=name,
        region=os.environ.get("IX_REGION") or "us-west-1",
        secret_files={secret: "runner-token"},
    )
    await asyncio.wait_for(ix.machines().create(options), CREATE_TIMEOUT)


async def reconcile(ix) -> int:
    """Converge the pool; return the number of creations/replacements."""
    pat = os.environ["RUNNER_PAT"]
    repo = os.environ["GITHUB_REPOSITORY"]
    pool = pool_name()
    os.environ.setdefault("IX_REGION", "us-west-1")
    # POOL_SIZE x `slots` runner daemons each = the concurrent job budget
    # (the consuming flake's mkPool size and this must agree).
    pool_size = int(os.environ.get("POOL_SIZE") or 8)
    max_replacements = int(os.environ.get("MAX_REPLACEMENTS") or 2)

    rev = desired_rev()
    runners = github_api(pat, repo, "/actions/runners?per_page=100")["runners"]
    vms = {info.name: info.id for info in await ix.machines().list()}
    # Empty pool = first bootstrap: nothing exists to thrash, so the cap
    # protects nothing - raise it and build the whole pool in one run.
    empty = not any(f"{pool}-runner-{m}" in vms for m in range(1, pool_size + 1))
    if empty and max_replacements < pool_size:
        print(f"empty pool -> bootstrap: raising the cap to {pool_size}")
        max_replacements = pool_size
    replaced = 0

    def budget() -> bool:
        if replaced >= max_replacements:
            print(
                f"replacement budget ({max_replacements}) exhausted;"
                " remaining members reconcile next run"
            )
            return False
        return True

    failures = 0

    async def make(member: int, name: str) -> None:
        # The budget is spent even when the create FAILS: a bad template
        # rev must stall after N attempts, not thrash the whole pool.
        nonlocal replaced, failures
        replaced += 1
        try:
            await create(ix, pat, repo, rev, pool, member, name)
        except (TimeoutError, OSError, RuntimeError) as e:
            failures += 1
            print(f"{name}: create FAILED ({e}); reconciling again next run")

    async def replace(member: int, name: str) -> None:
        # Deregister BEFORE deleting the VM: a busy registration (422)
        # means the member picked up a job since the scan - skip it this
        # round rather than kill the job. No budget is spent on a skip.
        if not deregister_member(pat, repo, runners, pool, member):
            print(f"{name}: picked up a job mid-scan -> deferred")
            return
        await ix.machines().connect(vms[name]).delete()
        await make(member, name)

    for member in range(1, pool_size + 1):
        name = f"{pool}-runner-{member}"
        if name not in vms:
            if not budget():
                break
            print(f"{name}: missing -> create")
            await make(member, name)
            continue
        machine = ix.machines().connect(vms[name])
        actual = await member_rev(machine)
        if actual is None:
            if not budget():
                break
            print(f"{name}: unreachable -> replace")
            await replace(member, name)
            continue
        if actual != rev:
            # Never roll a member out from under a running job: config
            # rolls wait for idleness, this member converges on a later run.
            if member_busy(runners, pool, member):
                print(f"{name}: stale rev but busy -> deferred")
                continue
            if not budget():
                break
            print(f"{name}: rev {actual[:12]} != {rev[:12]} -> replace")
            await replace(member, name)
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
            if not budget():
                break
            print(f"{name}: still offline after repair -> replace")
            await replace(member, name)
        else:
            print(f"{name}: runners offline -> repair (restart units)")
            await guest(machine, "systemctl", "restart", "github-runner-*")
            await guest(machine, "touch", REPAIRED_MARKER)

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
