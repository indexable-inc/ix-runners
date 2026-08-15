"""Every GitHub REST call, and the matching rules that read its answers.

The admin PAT never leaves this module's callers by accident: the opener
here refuses redirects (urllib re-sends Authorization across a 30x, which
would hand a repo-admin token to whatever host the Location names) and the
API host is pinned rather than taken from the environment.
"""

from __future__ import annotations

import datetime
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from .report import clean, log_error, log_warning

# Ceiling on active workflow runs whose jobs are counted for demand.
MAX_DEMAND_RUNS = 100
# Recently-completed runs read to derive the idle clock. The window this
# buys has to be longer than IDLE_GRACE or a runner's absence from it proves
# nothing, which the scale-down checks before trusting it.
IDLE_SCAN_RUNS = 30

# Spacing between registration DELETEs; see the 422 note on deregister_member.
DEREGISTER_PAUSE = 1.0

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
