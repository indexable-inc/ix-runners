"""Reconcile the self-hosted ix runner pool to this repo's runner config.

Runs from CI on GITHUB-HOSTED runners only (never self-hosted: a runner VM
must never see IX_TOKEN) - refused at startup, see require_hosted_runner.
Secrets: IX_TOKEN (the ix account the VMs bill
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
    VM younger than BOOT_GRACE      -> skip (it is still building/booting)
    VM on a stale runner-config rev -> replace (DEFERRED while any of its
                                       runners is busy: a config roll must
                                       not kill running jobs)
    runners offline, VM reachable   -> repair (restart units);
                                       still offline on the NEXT run -> replace
    VM unreachable                  -> replace
    VM above POOL_SIZE              -> prune (a shrink's orphan still bills)

Three phases, deliberately: PROBE every member concurrently, DECIDE from
that snapshot in a deterministic (rotated) order, then EXECUTE the admitted
actions concurrently. Probing sequentially made an all-unreachable pool pay
EXEC_TIMEOUT per member before anything happened.

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
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

# Paths whose last-touching commit defines the desired runner-config rev.
CONFIG_PATHS = ["nix/", "flake.nix", "flake.lock"]
# Baked into the image by the flake (specialArgs self.rev ->
# /etc/ix-runner/rev); read back here for the staleness check.
REV_PATH = "/etc/ix-runner/rev"
# Two-strike marker, recorded on the VM itself so this script stays
# stateless across runs.
REPAIRED_MARKER = "/var/lib/ix-runner/repaired"
# What the probe script prints when the marker is there; never a valid rev.
STRIKE = "ix-runner-strike"
# A wedged VM must not hang the reconcile.
EXEC_TIMEOUT = 60
# Bounds a create: a first boot of a new rev builds the template in-guest.
CREATE_TIMEOUT = 1800
# A machine this young is still compiling its template or booting, so its
# silence says nothing about its health. MachineInfo has no "building"
# status, so age is the only signal there is.
BOOT_GRACE = CREATE_TIMEOUT
# Spacing between registration DELETEs; see the 422 note on deregister_member.
DEREGISTER_PAUSE = 1.0


def log_error(message: str) -> None:
    """An Actions error annotation - surfaced on the run, not buried in logs."""
    print(f"::error::{message}")


def log_warning(message: str) -> None:
    """An Actions warning annotation."""
    print(f"::warning::{message}")


def write_summary(rows: list[tuple[str, str, str]]) -> None:
    """Append a per-member outcome table to the job summary, when in Actions."""
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path or not rows:
        return
    lines = ["", "| member | action | outcome |", "| --- | --- | --- |"]
    # An exception's repr can carry a pipe, which would break the table open.
    lines += [
        "| " + " | ".join(cell.replace("|", "\\|") for cell in row) + " |" for row in rows
    ]
    with open(path, "a", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")


def client() -> Any:
    """The ix API client; resolves IX_TOKEN from the environment."""
    from ix_sdk import Client

    return Client()


def error_body(error: urllib.error.HTTPError) -> str:
    """The failed response's body; empty when it carried none."""
    try:
        return error.read().decode("utf-8", "replace")
    except (AttributeError, OSError, ValueError):
        return ""


class RefuseRedirects(urllib.request.HTTPRedirectHandler):
    """Never follow a redirect on a PAT-bearing request.

    urllib keeps the Authorization header across a 30x (requests and urllib3
    strip it on a cross-host hop; urllib does not), so one redirect on any of
    these calls hands RUNNER_PAT - Administration rw, which is repo takeover -
    to whatever host the Location names. None of the endpoints we call
    legitimately redirects.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # None means "not handled": urllib then raises the 30x as an HTTPError
        # instead of chasing it, which github_api turns into a clear failure.
        return None


# One opener for every GitHub call, so the refusal cannot be bypassed by
# reaching for urlopen (which uses the default, redirect-following opener).
OPENER = urllib.request.build_opener(RefuseRedirects)
GITHUB_API_DEFAULT = "https://api.github.com"


def api_base() -> str:
    """The GitHub REST base URL, pinned to api.github.com.

    GITHUB_API_URL is only an environment variable, and any earlier step in
    the caller's job can rewrite the environment through $GITHUB_ENV: honored
    unconditionally, it aims the Bearer PAT at a host of that step's
    choosing. On github.com the value is always api.github.com, so pinning it
    costs nothing. A GHES/ARC deployment is exactly the deployment that
    already had to set IX_RUNNERS_ALLOW_NON_HOSTED, so its base is honored
    there - https only.
    """
    if os.environ.get("IX_RUNNERS_ALLOW_NON_HOSTED") == "1":
        return (os.environ.get("GITHUB_API_URL") or GITHUB_API_DEFAULT).rstrip("/")
    return GITHUB_API_DEFAULT


def api_url(repo: str, path: str) -> str:
    """The absolute REST URL for a repo path, with the host pinned.

    The resolved request host must be exactly the base's: nothing assembled
    from a repo name or a path may move a PAT-bearing call to another host.
    """
    base = api_base()
    parsed = urllib.parse.urlsplit(base)
    if parsed.scheme != "https" or not parsed.hostname:
        log_error(
            f"GITHUB_API_URL is {base!r}, which is not an https URL."
            " Refusing to send RUNNER_PAT to it."
        )
        raise SystemExit(1)
    url = f"{base}/repos/{repo}{path}"
    resolved = urllib.parse.urlsplit(url)
    if resolved.scheme != parsed.scheme or resolved.netloc != parsed.netloc:
        log_error(
            f"a GitHub API request resolved to {resolved.scheme}://{resolved.netloc},"
            f" not the configured {parsed.scheme}://{parsed.netloc}."
            " Refusing to send RUNNER_PAT to it."
        )
        raise SystemExit(1)
    return url


def github_api(
    pat: str, repo: str, path: str, *, method: str = "GET"
) -> dict[str, Any]:
    """Call the GitHub REST API with the PAT; return the parsed JSON body."""
    request = urllib.request.Request(
        api_url(repo, path),
        method=method,
        headers={"Authorization": f"Bearer {pat}"},
    )
    try:
        with OPENER.open(request, timeout=30) as response:
            body = response.read()
            return json.loads(body) if body else {}
    except urllib.error.HTTPError as error:
        # A 30x reaches here because RefuseRedirects declined it; say why,
        # rather than leaving a bare "HTTP Error 302" in the log.
        if 300 <= error.code < 400:
            raise RuntimeError(
                f"GitHub answered {method} {path} with HTTP {error.code}, a"
                " redirect. Refusing to follow it: urllib re-sends the"
                " Authorization header, which would hand RUNNER_PAT to the"
                " redirect target."
            ) from error
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


def list_runners(pat: str, repo: str) -> list[dict[str, Any]]:
    """Every self-hosted runner registered on the repo, across ALL pages.

    A short read is not merely incomplete, it is destructive: a member whose
    runners fall off the end of page one reads offline and gets replaced, so
    an unpaginated listing mass-replaces the pool the moment it passes 100
    registrations (POOL_SIZE x slots).
    """
    runners: list[dict[str, Any]] = []
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


def attr_prefix() -> str:
    """Flake attribute prefix for pool members, matching mkPool's attrPrefix."""
    return os.environ.get("ATTR_PREFIX") or "ci-runner"


def member_runners(
    runners: list[dict[str, Any]], pool: str, member: int
) -> list[dict[str, Any]]:
    """Every runner daemon registration belonging to pool member N.

    One VM runs `slots` daemons named `<pool>-r<N>-<slot>`; the trailing dash
    is what keeps member 1 from matching member 10.
    """
    prefix = f"{pool}-r{member}-"
    return [runner for runner in runners if runner["name"].startswith(prefix)]


def member_online(runners: list[dict[str, Any]], pool: str, member: int) -> bool:
    """Any runner daemon of pool member N online?"""
    return any(
        runner["status"] == "online" for runner in member_runners(runners, pool, member)
    )


def member_busy(runners: list[dict[str, Any]], pool: str, member: int) -> bool:
    """Any runner daemon of pool member N mid-job?"""
    return any(runner.get("busy") for runner in member_runners(runners, pool, member))


def extra_members(names: list[str], pool: str, pool_size: int) -> list[int]:
    """Pool members above POOL_SIZE: orphans of a shrink, still billing."""
    pattern = re.compile(rf"^{re.escape(pool)}-runner-(\d+)$")
    matches = (pattern.match(name) for name in names)
    return sorted(
        index
        for index in (int(m.group(1)) for m in matches if m)
        if index > pool_size
    )


# GitHub documents 422 on the runner-delete endpoint only as "Validation
# failed, or the endpoint has been spammed"; that a BUSY runner refuses
# deletion with it is undocumented community knowledge, and a
# secondary-rate-limit 422 wears exactly the same code. Read the body before
# believing "busy", or a rate-limited burst reads as a wholly idle pool.
BUSY_REFUSAL = ("busy", "running a job", "job is still running")


def is_busy_refusal(body: str) -> bool:
    """Does this 422 body say the runner is mid-job, rather than spam?"""
    lowered = body.lower()
    return any(hint in lowered for hint in BUSY_REFUSAL)


def deregister_member(
    pat: str,
    repo: str,
    runners: list[dict[str, Any]],
    pool: str,
    member: int,
) -> bool:
    """Delete pool member N's runner registrations; False when it is busy.

    Busy is checked across ALL of the member's slots BEFORE any delete:
    deleting until a 422 stops us leaves a half-deregistered VM that still
    reads healthy (member_online is any-slot-online) and serves at reduced
    capacity forever.

    Deregistering before the VM delete remains the lock - GitHub refuses to
    delete a busy runner - and the freshly-listed `busy` field closes the
    wide window; only a seconds-old assignment can still slip through, at
    the cost of one job retry.

    Blocking urllib: the caller runs this in a thread, under a lock.
    """
    registrations = member_runners(runners, pool, member)
    if any(runner.get("busy") for runner in registrations):
        return False
    for index, runner in enumerate(registrations):
        if index:
            # Rapid DELETEs trip GitHub's secondary rate limit.
            time.sleep(DEREGISTER_PAUSE)
        try:
            github_api(pat, repo, f"/actions/runners/{runner['id']}", method="DELETE")
        except urllib.error.HTTPError as error:
            if error.code == 404:  # already gone
                continue
            body = error_body(error)
            if error.code == 422 and is_busy_refusal(body):
                if index:
                    log_warning(
                        f"{pool}-runner-{member}: took a job mid-deregister -"
                        f" {index} of {len(registrations)} registration(s) are"
                        " ALREADY DELETED. It is half-deregistered and serving"
                        " at reduced capacity; a later run sees the missing"
                        " registrations and replaces it."
                    )
                return False
            raise RuntimeError(
                f"deregistering {runner['name']} failed: HTTP {error.code} {body}"
            ) from error
    return True


async def probe_member(machine: Any, *, clear_marker: bool) -> tuple[str | None, bool]:
    """One guest round-trip: the baked rev, the strike marker, its removal.

    Folded into a single shell because the marker `rm` was a whole extra exec
    on every healthy member, for a file that is almost never there. Exit
    status is deliberately ignored: `test -f` sets it whenever the marker is
    absent, which is the ordinary case.
    """
    script = f"cat {REV_PATH}; test -f {REPAIRED_MARKER} && echo {STRIKE}"
    if clear_marker:
        script += f"; rm -f {REPAIRED_MARKER}"
    try:
        result = await asyncio.wait_for(machine.shell(script), EXEC_TIMEOUT)
    # IxError subclasses RuntimeError; TimeoutError covers the wait_for bound.
    except (TimeoutError, OSError, RuntimeError):
        return None, False
    tokens = result.stdout.split()
    rev = next((token for token in tokens if token != STRIKE), None)
    return rev, STRIKE in tokens


async def guest(machine: Any, *command: str) -> bool:
    """Run a command in the guest; True when it exited 0, False otherwise."""
    try:
        result = await asyncio.wait_for(machine.exec(list(command)), EXEC_TIMEOUT)
        return result.exit_code == 0
    # IxError subclasses RuntimeError; TimeoutError covers the wait_for bound.
    except (TimeoutError, OSError, RuntimeError):
        return False


def machine_age(info: Any) -> float | None:
    """Seconds since the machine row was created (the SDK reports epoch ms)."""
    created_ms = getattr(info, "created_at", None)
    if not created_ms:
        return None
    return time.time() - created_ms / 1000


def create_options(**kwargs: Any) -> Any:
    """Construct CreateMachineOptions; a seam so tests never import the SDK
    (the wheel is x86_64-only; the fakes stand in for it anyway)."""
    from ix_sdk import CreateMachineOptions

    return CreateMachineOptions(**kwargs)


async def create(
    ix: Any,
    repo: str,
    rev: str,
    secret_name: str,
    prefix: str,
    member: int,
    name: str,
) -> None:
    """Provision one pool member; the registration token is already stored.

    The token reaches the VM as a root-only file via the secret_files
    attach, present at first boot - no post-boot seeding step exists. An
    unbuilt (rev, attr) template compiles server-side on first boot
    (single-flight per pinned rev; idempotency_key is refused for flake-ref
    templates, so none is sent).
    """
    options = create_options(
        template=f"github:{repo}/{rev}#{prefix}-{member}",
        name=name,
        region=os.environ.get("IX_REGION") or "us-west-1",
        secret_files={secret_name: "runner-token"},
    )
    await asyncio.wait_for(ix.machines().create(options), CREATE_TIMEOUT)


async def reconcile(ix: Any) -> int:
    """Converge the pool; return the number of creations/replacements."""
    pat = os.environ["RUNNER_PAT"]
    repo = os.environ["GITHUB_REPOSITORY"]
    pool = pool_name()
    prefix = attr_prefix()
    # POOL_SIZE x `slots` runner daemons each = the concurrent job budget
    # (the consuming flake's mkPool size and this must agree).
    pool_size = int(os.environ.get("POOL_SIZE") or 8)
    max_replacements = int(os.environ.get("MAX_REPLACEMENTS") or 2)
    concurrency = int(os.environ.get("CONCURRENCY") or 4)
    secret_name = os.environ.get("SECRET_NAME") or f"{pool}_runner_reg_token"

    rev = desired_rev()
    runners = list_runners(pat, repo)
    vms = {info.name: info for info in await ix.machines().list()}
    # Empty pool = first bootstrap: nothing exists to thrash, so the cap
    # protects nothing - raise it and build the whole pool in one run.
    empty = not any(f"{pool}-runner-{m}" in vms for m in range(1, pool_size + 1))
    if empty and max_replacements < pool_size:
        print(f"empty pool -> bootstrap: raising the cap to {pool_size}")
        max_replacements = pool_size

    gate = asyncio.Semaphore(concurrency)

    # -- probe (concurrent) --
    async def probe(member: int) -> tuple[int, str | None, bool]:
        info = vms.get(f"{pool}-runner-{member}")
        if info is None:
            return member, None, False
        async with gate:
            # The marker clear rides the probe, so a healthy member costs one
            # round-trip; it is only ever sent to a member with a live runner.
            found, struck = await probe_member(
                ix.machines().connect(info.id),
                clear_marker=member_online(runners, pool, member),
            )
        return member, found, struck

    probed = await asyncio.gather(*(probe(m) for m in range(1, pool_size + 1)))
    state = {member: (found, struck) for member, found, struck in probed}

    replaced = 0
    failures = 0
    actions: list[tuple[str, int, str]] = []  # (kind, member, name)
    summary: list[tuple[str, str, str]] = []  # (member, action, outcome)

    def admit(kind: str, member: int, name: str) -> bool:
        # Budget is spent at ADMISSION: a bad template rev stalls after N
        # attempts even when every attempt fails.
        nonlocal replaced
        if replaced >= max_replacements:
            print(
                f"{name}: replacement budget ({max_replacements}) exhausted;"
                " reconciles next run"
            )
            summary.append((name, kind, "deferred (budget)"))
            return False
        replaced += 1
        actions.append((kind, member, name))
        return True

    # -- decide --
    # Rotate the start: with a fixed order, one permanently-broken low-numbered
    # member owns the whole budget run after run and nothing above it ever
    # converges. Budget exhaustion also skips a member rather than ending the
    # pass - repairs and marker clears above it are free work.
    run_number = int(os.environ.get("GITHUB_RUN_NUMBER") or 0)
    start = run_number % pool_size if pool_size else 0
    order = [(start + offset) % pool_size + 1 for offset in range(pool_size)]
    for member in order:
        name = f"{pool}-runner-{member}"
        info = vms.get(name)
        if info is None:
            print(f"{name}: missing -> create")
            admit("create", member, name)
            continue
        actual, struck = state[member]
        if actual is None:
            age = machine_age(info)
            if age is not None and age < BOOT_GRACE:
                print(f"{name}: {int(age)}s old, still building/booting -> skip")
                summary.append((name, "skip", "booting"))
                continue
            log_warning(
                f"{name}: unreachable (status {getattr(info, 'status', 'unknown')},"
                f" failure {getattr(info, 'failure_reason', None)}) -> replace"
            )
            admit("replace", member, name)
            continue
        if actual != rev:
            # Never roll a member out from under a running job: config
            # rolls wait for idleness, this member converges on a later run.
            if member_busy(runners, pool, member):
                print(f"{name}: stale rev but busy -> deferred")
                summary.append((name, "replace", "deferred (busy)"))
                continue
            print(f"{name}: rev {actual[:12]} != {rev[:12]} -> replace")
            admit("replace", member, name)
            continue
        if member_online(runners, pool, member):
            print(f"{name}: healthy")
            continue
        # Offline but reachable and on the right rev: repair once by
        # restarting the units (a configured runner re-registers from its
        # persisted state and needs no fresh token); replace only if a prior
        # run already repaired and it is STILL offline (two-strike, with the
        # strike recorded on the VM itself so this script stays stateless).
        if struck:
            print(f"{name}: still offline after repair -> replace")
            admit("replace", member, name)
            continue
        print(f"{name}: runners offline -> repair (restart units)")
        actions.append(("repair", member, name))

    # A shrunk POOL_SIZE orphans the members above it: they keep billing and
    # keep taking jobs from a config nobody reconciles. Prune them on budget.
    for member in extra_members(list(vms), pool, pool_size):
        name = f"{pool}-runner-{member}"
        log_warning(f"{name}: above POOL_SIZE ({pool_size}) -> deregister and delete")
        admit("prune", member, name)

    # -- execute --
    minted = False
    if any(kind in ("create", "replace") for kind, _, _ in actions):
        token = github_api(
            pat, repo, "/actions/runners/registration-token", method="POST"
        )["token"]
        # Mask BEFORE the token can reach any other output: for its one-hour
        # life it can register a runner that steals this repo's jobs. flush is
        # load-bearing - stdout to a pipe block-buffers, so an unflushed mask
        # can still be sitting in this process while the SDK call below writes
        # the token into a traceback on stderr.
        print(f"::add-mask::{token}", flush=True)
        await ix.secrets().set(secret_name, token)
        minted = True

    if actions:
        print(f"executing {len(actions)} action(s), concurrency {concurrency}")
        # Deregistrations are serialized across members: concurrent DELETEs
        # trip GitHub's secondary rate limit, whose 422 is indistinguishable
        # by status code from a busy runner's refusal.
        deregistering = asyncio.Lock()

        async def run_action(kind: str, member: int, name: str) -> None:
            nonlocal replaced, failures
            async with gate:
                try:
                    if kind == "repair":
                        machine = ix.machines().connect(vms[name].id)
                        await guest(machine, "systemctl", "restart", "github-runner-*")
                        await guest(machine, "touch", REPAIRED_MARKER)
                        summary.append((name, kind, "units restarted"))
                        return
                    if kind in ("replace", "prune"):
                        # Deregister at EXECUTE time, right before the delete,
                        # off the event loop: the blocking urllib calls would
                        # otherwise stall every sibling create's timeout budget.
                        async with deregistering:
                            freed = await asyncio.to_thread(
                                deregister_member, pat, repo, runners, pool, member
                            )
                        if not freed:
                            print(f"{name}: picked up a job mid-scan -> deferred")
                            summary.append((name, kind, "deferred (busy)"))
                            replaced -= 1
                            return
                        await ix.machines().connect(vms[name].id).delete()
                    if kind != "prune":
                        await create(ix, repo, rev, secret_name, prefix, member, name)
                    summary.append((name, kind, "ok"))
                # Any exception, not just the SDK's: an unforeseen one used to
                # abort the gather and cancel every sibling MID-CREATE.
                except Exception as error:
                    failures += 1
                    log_error(
                        f"{name}: {kind} FAILED ({error!r}); reconciling again next run"
                    )
                    summary.append((name, kind, f"FAILED: {error}"))

        outcomes = await asyncio.gather(
            *(run_action(*action) for action in actions), return_exceptions=True
        )
        # run_action swallows Exception itself, so anything surviving is a
        # BaseException; return_exceptions keeps it from cancelling siblings,
        # and it is still a failure.
        for (kind, _, name), outcome in zip(actions, outcomes):
            if isinstance(outcome, BaseException):
                failures += 1
                log_error(f"{name}: {kind} raised past the handler ({outcome!r})")
                summary.append((name, kind, f"FAILED: {outcome!r}"))

    if minted:
        # Spent registration tokens are dead within the hour and would
        # otherwise pile up in the secret store forever. Best effort only:
        # every VM that needed this one already has it as a boot-time file.
        try:
            await ix.secrets().delete(secret_name)
        except Exception as error:
            log_warning(f"could not delete the spent secret {secret_name} ({error!r})")

    print(f"reconcile done: {replaced} creation(s)/replacement(s), {failures} failed")
    write_summary(sorted(summary))
    if failures:
        raise SystemExit(1)
    return replaced


def require_hosted_runner() -> None:
    """Refuse to run anywhere but a GitHub-hosted runner.

    The docstring, the README and the action all say hosted-only; this is
    what makes it true. Nothing else does.
    """
    # GHES and ARC report their own RUNNER_ENVIRONMENT ("self-hosted"), so
    # their operators opt out here, having read the paragraph above.
    if os.environ.get("IX_RUNNERS_ALLOW_NON_HOSTED") == "1":
        return
    if os.environ.get("RUNNER_ENVIRONMENT") != "github-hosted":
        log_error(
            "refusing to run: RUNNER_ENVIRONMENT is"
            f" {os.environ.get('RUNNER_ENVIRONMENT') or 'unset'!r}, not"
            " 'github-hosted'. This is the control plane for the runner pool:"
            " it holds IX_TOKEN and a repo-admin PAT, and it manages the very"
            " machines a self-hosted runner would be, so running it on one"
            " hands both secrets to the thing they exist to control. Set"
            " runs-on: ubuntu-latest. On GHES/ARC, set"
            " IX_RUNNERS_ALLOW_NON_HOSTED=1 to accept that risk explicitly."
        )
        raise SystemExit(1)


def main() -> None:
    """Entry point: require a hosted runner and the secrets, then converge."""
    # Actions pipes stdout, so Python block-buffers it: without this, a
    # traceback on stderr overtakes the log lines that explain it, and a
    # ::add-mask:: can trail the output it was meant to mask.
    sys.stdout.reconfigure(line_buffering=True)
    require_hosted_runner()
    for required in ("IX_TOKEN", "RUNNER_PAT", "GITHUB_REPOSITORY"):
        if not os.environ.get(required):
            raise SystemExit(f"{required} is required")
    asyncio.run(reconcile(client()))


if __name__ == "__main__":
    main()
