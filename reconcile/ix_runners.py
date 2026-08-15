"""Reconcile the self-hosted ix runner pool to this repo's runner config.

Runs from CI on GITHUB-HOSTED runners only - never self-hosted, since a
runner VM must never see IX_TOKEN, and require_hosted_runner refuses at
startup rather than trusting the caller. Secrets: IX_TOKEN (the ix account
the VMs bill to), RUNNER_PAT (fine-grained, "administration" repo rw). The
PAT never leaves that runner: it mints SHORT-LIVED (1 h) registration tokens
and reads runner status, over a host-pinned, redirect-refusing opener; a VM
only ever receives a registration token, which can do nothing but register a
runner and is dead within the hour.

Provisioning is pure ix Python SDK (ix-sdk on PyPI, pinned in the
entrypoint's inline metadata) - no CLI, no output parsing. The
registration token rides ``secrets().set()`` plus a ``secret_files``
attach at CREATE time, so it is present at first boot as a root-only file
and no post-boot seeding step exists; unbuilt templates compile
server-side on first boot.

Per pool member:
    missing VM                      -> create
    VM status failed                -> replace (a platform verdict, not a
                                       slow boot: BOOT_GRACE does not apply)
    VM stopped                      -> parked by autoscaling; never probed
                                       and never replaced for its silence
    VM younger than BOOT_GRACE      -> skip (it is still building/booting)
    VM on a stale runner-config rev -> replace (DEFERRED while any of its
                                       runners is busy: a config roll must
                                       not kill running jobs)
    runners offline, VM reachable   -> repair (restart units);
                                       still offline on the NEXT run -> replace
    VM unreachable                  -> replace
    VM above POOL_SIZE              -> prune (a shrink's orphan still bills)

AUTOSCALING is a second, independent axis, and it is LEVEL-BASED: every
tick observes the world fresh and moves it toward a level, rather than
replaying events or remembering what it did last time. Nothing is stored
between runs - not an idle counter, not a pending action, nothing - so a
missed tick costs latency and never correctness, and two reconciles cannot
hold different beliefs about the pool.

The member set is FIXED and declarative: POOL_SIZE machines, named and
built from the repo's runner config. Power state is the only thing that
moves. A stopped machine keeps its disk and bills storage alone.

    desired_online = clamp(ceil(servable_jobs / slots) + SCALE_HEADROOM,
                           MIN_WARM, MAX_ONLINE)

SERVABLE is strict: a job counts only when some runner in this pool
advertises EVERY label in its runs-on. GitHub ANDs those labels, and a repo
routinely has jobs queued forever against labels nothing here carries;
counting them would pin the pool at MAX_ONLINE on work it cannot do.

The GitHub queue is the buffer. Below the level, stopped members START -
always before a create, which is a template build rather than a boot, and
never rationed, because being short of capacity is the state with a queue
behind it. Above it, idle members STOP, highest index first, so the warm
core stays the same low-numbered machines with the hottest caches.

Two rules keep a wave from being throttled by its own arrival:

  * only a SCHEDULED tick may scale down. An event tick fires when a run is
    requested - before its jobs are in the queue - so it sees an idle pool
    at the moment a wave is landing.
  * a tick that cannot read GitHub makes NO scaling decision. Missing data
    is not zero demand, and it is not zero idleness either.

Scale-down DEREGISTERS before it cuts power. GitHub refuses (422) to delete
a runner mid-job, and that refusal is the only real lock here: after the
DELETE the runner cannot be assigned anything, which no amount of
re-reading a listing can guarantee. The price is that waking a member needs
a fresh registration token, which is why a start rotates the secret.

MIN_WARM defaults to POOL_SIZE, which is autoscaling off; nothing scales,
and no tick even reads the queue, until a pool dials it down.

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
import datetime
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
# The probe's whole legitimate answer is a 40-char rev plus a marker word.
# Anything past this is a fault or a hostile guest, and machine.shell()
# buffers the lot in this process, so the cap has to be here, client-side.
MAX_PROBE_OUTPUT = 4096
# Bounds a create: a first boot of a new rev builds the template in-guest.
CREATE_TIMEOUT = 1800
# A machine this young is still compiling its template or booting, so its
# silence says nothing about its health. MachineInfo has no "building"
# status, so age is the only signal there is.
BOOT_GRACE = CREATE_TIMEOUT
# Spacing between registration DELETEs; see the 422 note on deregister_member.
DEREGISTER_PAUSE = 1.0
# How old a member may get while its stale-rev replacement keeps deferring
# before the run says so out loud. Generous: a healthy pool replaces members
# far sooner, so reaching this at all means something never converges.
MAX_LIFETIME = 30 * 24 * 60 * 60
# A machine started this recently is WARMING: the platform reports Running
# the moment it is coming up (MachineStatus has no "starting" state), but its
# runner daemons take a few more seconds to register. Without this window a
# machine autoscaling started reads as offline-and-reachable and gets
# "repaired" on the spot, and a second tick starts it all over again.
WARM_GRACE = 300
# Ceiling on active workflow runs whose jobs are counted for demand.
MAX_DEMAND_RUNS = 100
# Recently-completed runs read to derive the idle clock. The window this
# buys has to be longer than IDLE_GRACE or a runner's absence from it proves
# nothing, which the scale-down checks before trusting it.
IDLE_SCAN_RUNS = 30


# A newline in a log line opens a fresh line, where `::` workflow commands
# parse and where a summary table takes another row. Plenty of what we print
# is chosen remotely: a runner's name, an ix failure_reason or status, an
# error message quoting either.
CONTROL_CHARACTERS = re.compile(r"[\x00-\x1f\x7f-\x9f]")


def clean(value: Any) -> str:
    """One line, safe to print: a remote string cannot forge output."""
    return CONTROL_CHARACTERS.sub(" ", str(value))


def log_error(message: str) -> None:
    """An Actions error annotation - surfaced on the run, not buried in logs."""
    print(f"::error::{clean(message)}")


def log_warning(message: str) -> None:
    """An Actions warning annotation."""
    print(f"::warning::{clean(message)}")


def write_summary(rows: list[tuple[str, str, str]]) -> None:
    """Append a per-member outcome table to the job summary, when in Actions."""
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path or not rows:
        return
    lines = ["", "| member | action | outcome |", "| --- | --- | --- |"]
    # A cell carries remote text: a pipe would break the table open and a
    # newline would forge a whole row of it.
    lines += [
        "| " + " | ".join(clean(cell).replace("|", "\\|") for cell in row) + " |"
        for row in rows
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
    token: str, repo: str, path: str, *, method: str = "GET", pat: bool = True
) -> dict[str, Any]:
    """Call the GitHub REST API; return the parsed JSON body.

    `pat` says whether `token` is the admin PAT, which only decides how a
    401 is reported: an expired PAT is the end of the run, while the
    workflow token merely losing a read is for the caller to survive.
    """
    request = urllib.request.Request(
        api_url(repo, path),
        method=method,
        headers={"Authorization": f"Bearer {token}"},
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
        if error.code == 401 and pat:
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


def runner_label_sets(
    runners: list[dict[str, Any]], pool: str, pool_size: int
) -> list[set[str]]:
    """What each of THIS pool's runners advertises it can serve.

    Read off the registrations rather than configured, because GitHub's own
    matching rule is defined over exactly these sets and nothing else.
    """
    sets = []
    for member in range(1, pool_size + 1):
        for runner in member_runners(runners, pool, member):
            labels = {
                str(label.get("name") or "")
                for label in (runner.get("labels") or [])
                if label.get("name")
            }
            if labels:
                sets.append(labels)
    return sets


def pool_can_serve(job: dict[str, Any], label_sets: list[set[str]]) -> bool:
    """Could any runner in this pool pick this job up?

    GitHub ANDs the labels in `runs-on`: a runner is eligible when it carries
    EVERY one of them. So the test is subset, not intersection, and it has to
    be per runner - two runners that between them cover a job's labels cannot
    run it.

    This is the difference between a demand signal and a stuck-queue
    amplifier. The repo has runs queued indefinitely against labels no ix
    runner has (blacksmith-*, macos-*, windows-*): jobs that will never be
    served by anything here. Counting them is not conservative, it is a
    permanent maximum - the pool would be pinned at max-online forever by
    work it cannot do.
    """
    wanted = {
        str(label) for label in (job.get("labels") or []) if isinstance(label, str)
    }
    if not wanted:
        return False
    return any(wanted <= advertised for advertised in label_sets)


def run_ids(token: str, repo: str, status: str, cap: int) -> tuple[list[int], bool]:
    """Ids of runs in one status, newest first; True when the cap was hit."""
    ids: dict[int, None] = {}
    seen = 0
    page = 1
    # One PAST the cap, so exactly `cap` runs is a complete answer rather than
    # one that looks truncated.
    while len(ids) <= cap:
        body = github_api(
            token,
            repo,
            f"/actions/runs?status={status}&per_page=100&page={page}",
            pat=False,
        )
        batch = body.get("workflow_runs") or []
        if not batch:
            break
        ids.update((int(run["id"]), None) for run in batch)
        seen += len(batch)
        if seen >= int(body.get("total_count") or 0):
            break
        page += 1
    return list(ids)[:cap], len(ids) > cap


def run_jobs(token: str, repo: str, run_id: int) -> list[dict[str, Any]]:
    """Every job of one run, across pages."""
    jobs: list[dict[str, Any]] = []
    page = 1
    while True:
        body = github_api(
            token,
            repo,
            f"/actions/runs/{run_id}/jobs?filter=latest&per_page=100&page={page}",
            pat=False,
        )
        batch = body.get("jobs") or []
        jobs.extend(batch)
        if len(batch) < 100:
            break
        page += 1
    return jobs


def parse_time(value: Any) -> float | None:
    """An ISO-8601 GitHub timestamp as epoch seconds, or None."""
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


class Observation:
    """Everything the scaler learned from GitHub in one tick.

    One object because the parts are only meaningful together: a demand
    number without the idle window can scale up but must not scale down.
    """

    def __init__(
        self,
        demand: int,
        idle_since: dict[str, float],
        window_start: float | None,
        truncated: bool,
    ) -> None:
        self.demand = demand
        # runner name -> when it last finished a job (epoch seconds).
        self.idle_since = idle_since
        # Oldest job completion the scan saw. Absence of a runner from
        # idle_since only proves it has been idle since HERE.
        self.window_start = window_start
        # More active runs than the scan will read: the demand number is a
        # floor, not a count.
        self.truncated = truncated

    def idle_for(self, names: list[str], now: float) -> float | None:
        """How long every one of these runners has been idle, or None when
        the scan cannot say."""
        latest = max(
            (self.idle_since[name] for name in names if name in self.idle_since),
            default=None,
        )
        if latest is not None:
            return now - latest
        # Never seen finishing a job in the window. That means idle since at
        # least the start of the window - but only if there IS a window.
        if self.window_start is None:
            return None
        return now - self.window_start


def observe(
    token: str, repo: str, label_sets: list[set[str]], idle_scan_runs: int
) -> Observation:
    """One pass over GitHub's job queue: demand, and when runners last worked.

    Active runs give demand. Recently-completed runs give the idle clock -
    derived, not stored, so the reconcile keeps no state between ticks and
    two reconciles cannot disagree about how long something has been idle.
    """
    active: list[int] = []
    truncated = False
    for status in ("queued", "in_progress"):
        ids, hit_cap = run_ids(token, repo, status, MAX_DEMAND_RUNS)
        active.extend(ids)
        truncated = truncated or hit_cap

    demand = 0
    for run_id in dict.fromkeys(active):
        for job in run_jobs(token, repo, run_id):
            if job.get("status") in ("queued", "in_progress") and pool_can_serve(
                job, label_sets
            ):
                demand += 1

    idle_since: dict[str, float] = {}
    window_start: float | None = None
    completed, _ = run_ids(token, repo, "completed", idle_scan_runs)
    for run_id in completed:
        for job in run_jobs(token, repo, run_id):
            finished = parse_time(job.get("completed_at"))
            name = job.get("runner_name")
            if finished is None:
                continue
            window_start = (
                finished if window_start is None else min(window_start, finished)
            )
            if isinstance(name, str) and name:
                idle_since[name] = max(idle_since.get(name, 0.0), finished)

    return Observation(demand, idle_since, window_start, truncated)


def observe_or_none(
    token: str, repo: str, label_sets: list[set[str]], idle_scan_runs: int
) -> Observation | None:
    """observe(), except that a failed read is None rather than an exception.

    None means "this tick learned nothing", and the caller's answer to that
    is to make no scaling decision at all. Missing data is not zero demand,
    and it is not zero idleness either: guessing in EITHER direction on an
    unread queue is how a pool stops the work it should be doing, or stops
    machines that are about to be handed a job.

    Broad on purpose. HTTPError subclasses OSError, which also covers a reset
    connection, a DNS failure and a TLS error; RuntimeError is the redirect
    refusal. This runs before the execute phase, so an escaping exception
    would discard every create, replace and repair the run had decided on.
    """
    try:
        return observe(token, repo, label_sets, idle_scan_runs)
    except (OSError, RuntimeError) as error:
        code = getattr(error, "code", None)
        hint = (
            " The workflow's token needs `actions: read`."
            if code in (401, 403, 404)
            else ""
        )
        log_warning(
            f"could not read the job queue ({code or type(error).__name__})."
            f"{hint} Making no scaling decision this tick."
        )
        return None
def pool_slots(runners: list[dict[str, Any]], pool: str, pool_size: int) -> int:
    """Runner daemons per VM, read back off the registrations.

    The nix module registers one daemon per configured slot, so the widest
    member IS the configured value - which is why this is a max: a member
    caught mid-deregister reports fewer daemons, never more, and reading the
    pool as narrower than it is would over-provision on every wave.

    Offline registrations count too, deliberately: in the steady state most
    of the pool is parked, and a parked member's registrations are all
    offline. The cost is that REDUCING `slots` reads high until every member
    has rolled, because the removed slots stay registered (the module says as
    much), so a wave in that window is under-provisioned by the old ratio.
    That is a slower wave during one config roll, against reading every
    parked pool as one slot wide, which would over-provision permanently.
    """
    widest = max(
        (
            len(member_runners(runners, pool, member))
            for member in range(1, pool_size + 1)
        ),
        default=0,
    )
    return max(widest, 1)


def machine_status(info: Any) -> str:
    """The machine's lifecycle status, lowercased.

    MachineStatus is a StrEnum whose values are lowercase, but a record built
    by hand elsewhere can carry a plain string of any case. Every comparison
    goes through here, because one capital letter would read a STOPPED member
    as an ordinary running one, probe it, get silence, and delete it.
    """
    return str(getattr(info, "status", "") or "").lower()


def started_recently(info: Any, within: float = WARM_GRACE) -> bool:
    """Did this machine start within the last `within` seconds?

    `started_at` is absent on a machine that has never started, which is not
    recent by any reading.
    """
    started_ms = getattr(info, "started_at", None)
    if not started_ms:
        return False
    return time.time() - started_ms / 1000 < within


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
            # Report the status and the runner name, never the response body.
            # The body is the reply to a RUNNER_PAT-authenticated request, so
            # anything derived from it is treated as secret-tainted; keeping it
            # out of logs both satisfies clear-text-logging scanners and keeps
            # authenticated response bodies off a public repo's run logs. The
            # 422 shape we act on (busy) is already classified above.
            raise RuntimeError(
                f"deregistering {clean(runner['name'])} failed: HTTP {error.code}"
            ) from error
    return True


async def probe_member(machine: Any, *, clear_marker: bool) -> tuple[str | None, bool]:
    """One guest round-trip: the baked rev, the strike marker, its removal.

    Folded into a single shell because the marker `rm` was a whole extra exec
    on every healthy member, for a file that is almost never there. Exit
    status is deliberately ignored: `test -f` sets it whenever the marker is
    absent, which is the ordinary case.

    Every failure reads as "unreachable", which is a state the decide loop
    already handles. The guest is the least trusted thing here: it answers
    with whatever it likes, so nothing it says may end the run.
    """
    script = f"cat {REV_PATH}; test -f {REPAIRED_MARKER} && echo {STRIKE}"
    if clear_marker:
        script += f"; rm -f {REPAIRED_MARKER}"
    try:
        result = await asyncio.wait_for(machine.shell(script), EXEC_TIMEOUT)
    # Broad on purpose: IxError subclasses RuntimeError and TimeoutError
    # covers the wait_for bound, but a MemoryError from a huge reply is
    # neither, and one member must never take the whole reconcile down.
    except Exception:
        return None, False
    stdout = result.stdout or ""
    if len(stdout) > MAX_PROBE_OUTPUT:
        # A `head -c` inside the script bounds nothing - the guest chooses
        # what it sends, and machine.shell() buffers all of it here.
        log_warning(
            f"a pool member answered the probe with {len(stdout)} bytes"
            f" (cap {MAX_PROBE_OUTPUT}); treating it as unreachable"
        )
        return None, False
    tokens = stdout.split()
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
    # Mask the admin PAT for the whole run, as we do the registration token: it
    # is never deliberately printed, but this redacts it from any traceback the
    # runner emits. flush so the directive lands before anything it must cover.
    print(f"::add-mask::{pat}", flush=True)
    repo = os.environ["GITHUB_REPOSITORY"]
    pool = pool_name()
    prefix = attr_prefix()
    # POOL_SIZE x `slots` runner daemons each = the concurrent job budget
    # (the consuming flake's mkPool size and this must agree).
    pool_size = int(os.environ.get("POOL_SIZE") or 8)
    max_replacements = int(os.environ.get("MAX_REPLACEMENTS") or 2)
    concurrency = int(os.environ.get("CONCURRENCY") or 4)
    secret_name = os.environ.get("SECRET_NAME") or f"{pool}_runner_reg_token"

    # -- autoscaling config --
    # MIN_WARM defaults to the whole pool: unset, every member stays on and
    # this is exactly the pre-autoscaling reconcile.
    min_warm = int(os.environ.get("MIN_WARM") or pool_size)
    max_online = int(os.environ.get("MAX_ONLINE") or pool_size)
    headroom = int(os.environ.get("SCALE_HEADROOM") or 2)
    # Seconds, not ticks: idle time is derived from GitHub's own job
    # timestamps, so it is a real duration and does not depend on how often
    # this runs. A tick counter meant the grace silently changed length
    # whenever the cron did.
    idle_grace = float(os.environ.get("IDLE_GRACE_SECONDS") or 600)
    # Only stops are capped. Being SHORT of capacity is the state with a
    # queue behind it, so a start is never rationed; being long of it costs
    # money, which can wait for the next tick.
    max_stops = int(os.environ.get("MAX_STOPS") or 4)
    runner_label = os.environ.get("RUNNER_LABEL") or ""
    # Which trigger this is. Only a scheduled tick may switch machines OFF:
    # an event tick fires when a run is REQUESTED, before its jobs reach the
    # queue, so it sees an idle pool at the exact moment a wave is landing.
    tick_mode = (
        os.environ.get("TICK_MODE")
        or (
            "scheduled"
            if os.environ.get("GITHUB_EVENT_NAME") in (None, "", "schedule", "workflow_dispatch")
            else "event"
        )
    ).lower()
    # The workflow's own token, which needs `actions: read` and nothing else.
    # Falling back to the PAT would work only if someone had granted it the
    # Actions permission, and the whole point is that it must not have one.
    demand_token = os.environ.get("GITHUB_TOKEN") or ""

    failures = 0
    # Demand can only move desired_online between these two, so when they
    # meet there is nothing to ask GitHub: desired IS max_online. That is the
    # default (both are POOL_SIZE), and it is why an unconfigured pool never
    # pays for a demand scan and never needs a label.
    autoscaling = min_warm < max_online

    def refuse_autoscaling(why: str) -> None:
        """Turn autoscaling off for this run, loudly, without ending it.

        Every other refusal here exits, because continuing would be
        destructive. This one is not: the safe reading of a broken scaling
        config is "keep the whole pool on", which is what the pool costs
        today. So the run goes red - a misconfiguration must not be quiet -
        but healing still happens, because a pool that stops being repaired
        is a worse outcome than a pool that is briefly too expensive.
        """
        nonlocal autoscaling, failures, min_warm, max_online
        log_error(f"autoscaling is off for this run: {why}")
        autoscaling = False
        failures += 1
        # Off means the WHOLE pool stays on, which is what the log line below
        # goes on to claim. Leaving a hand-set MAX_ONLINE in place would keep
        # stopping machines while announcing that it does not.
        min_warm = max_online = pool_size

    if not 0 <= min_warm <= max_online <= pool_size:
        refuse_autoscaling(
            f"MIN_WARM={min_warm}, MAX_ONLINE={max_online} and"
            f" POOL_SIZE={pool_size} must satisfy"
            " 0 <= MIN_WARM <= MAX_ONLINE <= POOL_SIZE"
        )
    if autoscaling:
        if not runner_label:
            refuse_autoscaling(
                "RUNNER_LABEL is unset, so there is no demand signal. Set it"
                " to the label your jobs target (one of"
                " `services.ix-runner.labels`). It is what a bootstrap pool"
                " matches jobs against before any runner has registered a"
                " label set of its own; without it a pool with nothing"
                " registered can serve no job it can see."
            )
        elif not demand_token:
            refuse_autoscaling(
                "GITHUB_TOKEN is unset, so the job queue cannot be read."
                " Pass the workflow's own token (github-token on the action)"
                " and give the job `permissions: actions: read`."
            )

    rev = desired_rev()
    runners = list_runners(pat, repo)
    # What this pool can actually serve, read off its own registrations so
    # GitHub's matching rule is applied against real advertised labels. A
    # pool with nothing registered yet (a bootstrap) has none to read, so
    # fall back to the declared label plus the implicit self-hosted one.
    label_sets = runner_label_sets(runners, pool, pool_size) or (
        [{"self-hosted", runner_label}] if runner_label else []
    )
    vms = {info.name: info for info in await ix.machines().list()}
    # Empty pool = first bootstrap: nothing exists to thrash, so the cap
    # protects nothing - raise it and build the whole pool in one run.
    empty = not any(f"{pool}-runner-{m}" in vms for m in range(1, pool_size + 1))
    if empty and max_replacements < pool_size:
        print(f"empty pool -> bootstrap: raising the cap to {pool_size}")
        max_replacements = pool_size

    gate = asyncio.Semaphore(concurrency)

    # -- probe (concurrent) --
    members = list(range(1, pool_size + 1))

    async def probe(member: int) -> tuple[int, str | None, bool]:
        info = vms.get(f"{pool}-runner-{member}")
        # A stopped or failed machine has no guest to answer, and the decide
        # loop reads silence as "unreachable -> replace". Probing one would
        # spend EXEC_TIMEOUT to learn nothing and then delete every machine
        # autoscaling had just parked; power state is read off the machine
        # row, never inferred from a guest that cannot speak.
        if info is None or machine_status(info) != "running":
            return member, None, False
        async with gate:
            try:
                # connect() and the marker decision are inside the try too:
                # outside it, a member that could not even be connected to
                # cancelled every sibling probe.
                machine = ix.machines().connect(info.id)
                # The marker clear rides the probe, so a healthy member costs
                # one round-trip; only a member with a live runner is sent it.
                found, struck = await probe_member(
                    machine, clear_marker=member_online(runners, pool, member)
                )
            except Exception as error:
                log_warning(
                    f"{pool}-runner-{member}: probe failed ({error!r})"
                    " -> reading it as unreachable"
                )
                return member, None, False
        return member, found, struck

    # return_exceptions: one member's failure decides that member, never the
    # whole run. An escaping exception cancelled every sibling probe and left
    # the pool - including a security rev bump - unconverged forever.
    probed = await asyncio.gather(
        *(probe(member) for member in members), return_exceptions=True
    )
    state: dict[int, tuple[str | None, bool]] = {}
    for member, outcome in zip(members, probed):
        if isinstance(outcome, BaseException):
            log_warning(
                f"{pool}-runner-{member}: probe raised past the handler"
                f" ({outcome!r}) -> reading it as unreachable"
            )
            state[member] = (None, False)
            continue
        _, found, struck = outcome
        state[member] = (found, struck)

    replaced = 0
    actions: list[tuple[str, int, str]] = []  # (kind, member, name)
    summary: list[tuple[str, str, str]] = []  # (member, action, outcome)
    # Power-state classification, filled by the decide loop below and read by
    # the scaling plan after it. A member appears in at most one of these.
    running: list[int] = []  # powered on, whatever its health
    online: list[int] = []  # running, with a runner registered online
    warming: list[int] = []  # running and young, runner not registered yet
    stopped: list[int] = []  # parked; startable in seconds

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
        status = machine_status(info)
        if status == "failed":
            # A platform verdict, not a slow boot: BOOT_GRACE exists because
            # a young machine's silence says nothing, and this machine is not
            # silent - it has told us it is dead. Waiting out the grace on it
            # is 30 minutes of a pool member that will never come back.
            log_warning(
                f"{name}: the platform reports it FAILED"
                f" ({clean(getattr(info, 'failure_reason', None))}) -> replace"
            )
            admit("replace", member, name)
            continue
        if status == "stopped":
            # Parked by autoscaling (or by hand). Its disk - and with it the
            # runner credentials - is intact, so it re-registers on boot; the
            # scaling plan below decides whether to wake it.
            print(f"{name}: stopped")
            stopped.append(member)
            continue
        if status != "running":
            # Not a status this version understands. Every branch below reads
            # a silent guest as "delete and rebuild", and a status we cannot
            # interpret is no evidence at all that the machine is dead.
            log_warning(f"{name}: unknown status {clean(status or 'none')} -> skip")
            summary.append((name, "skip", f"status {clean(status or 'none')}"))
            continue
        running.append(member)
        actual, struck = state[member]
        if actual is None:
            # A machine started moments ago answers nothing yet, and its AGE
            # says nothing about that: age is measured from creation, so a
            # member created last week and started twenty seconds ago sails
            # past BOOT_GRACE and gets deleted - taking with it the disk, and
            # the registration credentials that make a stop cheap in the
            # first place. Autoscaling creates this case on every scale-up,
            # and a wake-triggered reconcile lands right in it.
            if started_recently(info):
                print(f"{name}: started {int(WARM_GRACE)}s-fresh, guest not up yet -> warming")
                warming.append(member)
                summary.append((name, "skip", "warming"))
                continue
            age = machine_age(info)
            if age is not None and age < BOOT_GRACE:
                print(f"{name}: {int(age)}s old, still building/booting -> skip")
                summary.append((name, "skip", "booting"))
                continue
            log_warning(
                f"{name}: unreachable (status {clean(status or 'unknown')},"
                f" failure {clean(getattr(info, 'failure_reason', None))}) -> replace"
            )
            admit("replace", member, name)
            continue
        if actual != rev:
            # Never roll a member out from under a running job: config
            # rolls wait for idleness, this member converges on a later run.
            if member_busy(runners, pool, member):
                # ...unless it is never idle at scan time, in which case it
                # defers its own replacement forever and no runner-config
                # change - including a security one - ever reaches it. Say
                # so; do NOT force the replace. The execute-time deregister
                # refuses a busy member from the same snapshot, and bypassing
                # that check is what leaves a member half-deregistered and
                # serving at reduced capacity. A real drain (disable the
                # registrations, wait for idle, then replace) is the fix, and
                # it is a bigger change than this pass.
                age = machine_age(info)
                if age is not None and age > MAX_LIFETIME:
                    log_warning(
                        f"{name}: {int(age // 86400)} days old, on a stale rev,"
                        " and busy at every scan, so its replacement keeps"
                        " deferring and the current runner config has never"
                        " reached it. Drain it by hand: disable its runners in"
                        " the repo's Actions settings, let the jobs finish, and"
                        " re-run this workflow."
                    )
                print(f"{name}: stale rev but busy -> deferred")
                summary.append((name, "replace", "deferred (busy)"))
                continue
            # The rev is whatever the guest printed, so it is cleaned and
            # truncated before it reaches a log line.
            print(f"{name}: rev {clean(actual[:12])} != {rev[:12]} -> replace")
            admit("replace", member, name)
            continue
        if member_online(runners, pool, member):
            print(f"{name}: healthy")
            online.append(member)
            continue
        if started_recently(info):
            # Started within WARM_GRACE, so its runners are still coming up.
            # Repairing it here would restart units mid-registration, and the
            # scaling plan needs it counted as on-its-way-online or the next
            # tick starts a second machine for the same job.
            print(f"{name}: warming (started recently, runners not up yet)")
            warming.append(member)
            summary.append((name, "skip", "warming"))
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

    # -- scale --
    # Level-based, and stateless by construction: every number below is
    # observed fresh this tick from GitHub and the machine rows. Nothing is
    # remembered between runs, so two reconciles cannot disagree about what
    # the pool looked like, and a missed tick costs latency rather than
    # correctness.
    #
    # A member already booked for a create/replace/repair is not a power
    # candidate: stopping something mid-replace, or starting something whose
    # units are being restarted, races the action for no benefit.
    booked = {member for _, member, _ in actions}
    # Everything already powered on counts as capacity, not just the healthy.
    # A warming member is seconds from taking a job; one being repaired, or
    # deferred mid-config-roll, is up and serving right now. Members booked
    # for a replace or a prune do NOT count: they are about to go away.
    doomed = {member for kind, member, _ in actions if kind in ("replace", "prune")}
    effective = len([member for member in running if member not in doomed])

    seen: Observation | None = None
    if not autoscaling:
        # The clamp pins desired to max_online whatever demand says, so there
        # is nothing to ask GitHub. This is the default path: every member
        # that exists should be on, and one stopped by hand is STARTED rather
        # than deleted and rebuilt.
        desired = max_online
        why = "autoscaling off"
    else:
        seen = observe_or_none(demand_token, repo, label_sets, IDLE_SCAN_RUNS)
        if seen is None:
            # A tick that learned nothing makes NO scaling decision. Missing
            # data is not zero demand, and it is not zero idleness either:
            # guessing up strands nothing but costs money forever, guessing
            # down stops machines about to be handed a job. Healing above
            # still ran; only scaling is skipped.
            desired = effective
            why = "queue unreadable"
        else:
            slots = pool_slots(runners, pool, pool_size)
            # Jobs round UP into members: one leftover job still needs a
            # whole machine to run on.
            needed = -(-seen.demand // slots)
            desired = min(max(needed + headroom, min_warm), max_online)
            why = (
                f"{seen.demand} servable job(s)/{slots} slot(s) = {needed}"
                f" +{headroom} headroom, clamped [{min_warm},{max_online}]"
            )

    starts: list[int] = []
    stops: list[int] = []
    if effective < desired:
        # Start before create, always: a stopped member is a boot, a missing
        # one is a template build. Lowest index first, the mirror of the stop
        # order, so the warm core is a stable set of the same machines.
        # Deliberately NOT rate-capped: being short of capacity is the state
        # with a queue behind it, and every tick of delay is queued jobs.
        starts = sorted(member for member in stopped if member not in booked)[
            : desired - effective
        ]
    elif effective > desired and seen is not None:
        if tick_mode != "scheduled":
            # An event tick fires when a run is REQUESTED, which is the
            # moment before its jobs appear in the queue: the pool looks idle
            # precisely because the wave has not landed yet. Scaling down
            # here would switch machines off at the start of a wave. Event
            # ticks may only ever add capacity.
            print(f"{effective - desired} surplus member(s), but an event tick"
                  " never scales down")
        else:
            surplus = effective - desired
            now = time.time()
            # Never a busy one, and never a warming one (it has not had the
            # chance to take a job yet, and stopping it wastes the boot).
            # Highest index first: the low members stay warm, so their
            # template and toolchain caches stay hot.
            for member in sorted(online, reverse=True):
                if len(stops) >= min(surplus, max_stops):
                    break
                if member in booked or member_busy(runners, pool, member):
                    continue
                name = f"{pool}-runner-{member}"
                # Idle time is DERIVED: the last completion GitHub recorded
                # for any of this member's runners. No stored counter, no
                # consecutive-tick bookkeeping, nothing to get out of step.
                names = [
                    runner["name"] for runner in member_runners(runners, pool, member)
                ]
                idle_for = seen.idle_for(names, now)
                if idle_for is None:
                    # The scan saw no completions at all, so "never finished a
                    # job recently" is indistinguishable from "the window is
                    # empty". Not evidence of idleness.
                    print(f"{name}: no idle history in the scan -> not stopped")
                    continue
                if idle_for < idle_grace:
                    # Covers both shapes at once, which is why there is no
                    # separate window check: for a member the scan DID see
                    # finish, this is its real idle time; for one it did not,
                    # idle_for is the window's own length, so a window
                    # shorter than the grace fails here exactly as it should.
                    # Absence from a short window is not evidence of idleness.
                    print(
                        f"{name}: idle {int(idle_for)}s of {int(idle_grace)}s"
                        " grace -> not yet"
                    )
                    continue
                stops.append(member)

    for member in starts:
        actions.append(("start", member, f"{pool}-runner-{member}"))
    for member in stops:
        actions.append(("stop", member, f"{pool}-runner-{member}"))

    # One line per tick, so a reader can reconstruct the decision without
    # replaying the log: what was observed, what it implied, what was done.
    print(
        f"DECISION [{tick_mode}] powered_on={effective}"
        f" (online={len(online)} warming={len(warming)} stopped={len(stopped)})"
        f" demand={seen.demand if seen else 'n/a'}"
        f"{' (truncated)' if seen and seen.truncated else ''}"
        f" -> desired={desired} [{why}]"
        f" | start {sorted(starts)} stop {sorted(stops)}"
    )

    # -- execute --
    minted = False
    # A START needs one too, now that scale-down deregisters: the woken
    # machine has no registration left, so it must re-register at boot, and
    # it can only do that with a token it has never seen before (the runner
    # re-registers precisely BECAUSE the token file changed).
    if any(kind in ("create", "replace", "start") for kind, _, _ in actions):
        token = github_api(
            pat, repo, "/actions/runners/registration-token", method="POST"
        )["token"]
        # Mask BEFORE the token can reach any other output: for its one-hour
        # life it can register a runner that steals this repo's jobs. flush is
        # load-bearing - stdout to a pipe block-buffers, so an unflushed mask
        # can still be sitting in this process while the SDK call below writes
        # the token into a traceback on stderr.
        print(f"::add-mask::{token}", flush=True)
        # OVERWRITE the account secret; never delete-then-recreate it. The
        # platform propagates a rotation to every machine already holding a
        # copy - stopped ones included, where it lands in the machine's
        # secret row and is delivered at next boot - but ONLY when the write
        # updates an existing row. A write that INSERTS is a first write,
        # fires no rotation, and every pool member would go on booting with
        # the spent token from whenever it was created. Deleting the spent
        # secret at the end of a run, which is what this used to do, turns
        # every subsequent write into exactly that insert.
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
                    if kind == "start":
                        # The token was rotated into vm_secrets before this
                        # gather began; boot re-reads that table, so the guest
                        # comes up with the FRESH token and its runner
                        # re-registers from scratch. That is the whole reason
                        # a wake costs a token mint: scale-down deregistered
                        # it, so there is nothing left to reconnect with.
                        await ix.machines().connect(vms[name].id).start()
                        summary.append((name, kind, "started"))
                        return
                    if kind == "stop":
                        # DEREGISTER FIRST, then cut the power. GitHub refuses
                        # (422) to delete a runner that is mid-job, and that
                        # refusal is the only real lock in this system: it is
                        # atomic with respect to job assignment in a way no
                        # amount of re-reading a listing can be. Checking
                        # `busy` and then stopping leaves a window in which a
                        # job is assigned and then killed; deregistering
                        # closes it, because after the DELETE the runner
                        # cannot be assigned anything at all.
                        #
                        # The cost is that the stop is no longer free to undo:
                        # the registration is gone, so waking this member
                        # needs a fresh token. That is what the rotation
                        # before this gather pays for.
                        async with deregistering:
                            try:
                                freed = await asyncio.to_thread(
                                    deregister_member, pat, repo, runners, pool, member
                                )
                            except SystemExit:
                                # deregister_member reaches github_api, which
                                # exits the process on an expired PAT.
                                # SystemExit is a BaseException, so
                                # return_exceptions on the gather does NOT
                                # hold it - it would tear down every sibling
                                # mid-flight. A stop is the most abandonable
                                # action there is.
                                log_warning(
                                    f"{name}: deregister could not run -> left running"
                                )
                                summary.append((name, kind, "skipped (no listing)"))
                                return
                        if not freed:
                            print(f"{name}: took a job before the stop -> left running")
                            summary.append((name, kind, "skipped (busy)"))
                            return
                        await ix.machines().connect(vms[name].id).stop()
                        summary.append((name, kind, "stopped"))
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

        # STARTS (and every healing action) run to completion BEFORE any
        # stop begins, so a tick that dies halfway leaves the pool larger
        # than intended, never smaller: too much capacity is a bill, too
        # little is a stuck queue.
        #
        # As the scaler stands this is a guard rather than a live path - the
        # plan is a single if/elif on effective vs desired, so one tick emits
        # starts or stops and never both (pinned by a test). The split is
        # here so that stops cannot overtake starts if that ever changes.
        phases = [
            [action for action in actions if action[0] != "stop"],
            [action for action in actions if action[0] == "stop"],
        ]
        for phase in phases:
            if not phase:
                continue
            outcomes = await asyncio.gather(
                *(run_action(*action) for action in phase), return_exceptions=True
            )
            # run_action swallows Exception itself, so anything surviving is
            # a BaseException; return_exceptions keeps it from cancelling
            # siblings, and it is still a failure.
            for (kind, _, name), outcome in zip(phase, outcomes):
                if isinstance(outcome, BaseException):
                    failures += 1
                    log_error(f"{name}: {kind} raised past the handler ({outcome!r})")
                    summary.append((name, kind, f"FAILED: {outcome!r}"))

    # The spent token is deliberately LEFT in the secret store. It is dead
    # within the hour and can do nothing but register a runner on this one
    # repo, and keeping the row is what makes the next write a rotation
    # rather than an insert - which is the only way a stopped machine ever
    # receives a usable token again.

    # Counted off the summary, not off the plan: a stop that found its member
    # busy, or failed outright, did not change any power state.
    powered = sum(
        1
        for _, kind, outcome in summary
        if kind in ("start", "stop") and outcome in ("started", "stopped")
    )
    print(
        f"reconcile done: {replaced} creation(s)/replacement(s),"
        f" {powered} power change(s), {failures} failed"
    )
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
