"""Every knob, resolved once, and the rules that decide a knob is wrong.

Config is frozen because the rest of the run treats it as a fact. It is
built once, at the top, and the awkward part - a scaling range that does not
make sense - is resolved HERE rather than being re-checked at every use:
`refusals` carries what was wrong, and the values are already corrected to
the safe reading by the time anything reads them.

SECRETS ARE DELIBERATELY NOT IN HERE. A dataclass has a repr, and a repr
ends up in tracebacks; the admin PAT and the workflow token are passed
separately so no accident can print them.
"""

from __future__ import annotations

import dataclasses
import os
import subprocess

from .report import log_error

# Paths whose last-touching commit defines the desired runner-config rev.
CONFIG_PATHS = ["nix/", "flake.nix", "flake.lock"]

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


# Knobs that only exist so a test can drive them, never documented on the
# action: the trigger normally speaks for itself.
SCALE_DOWN_EVENTS = (None, "", "schedule", "workflow_dispatch")


def _int(name: str, default: int) -> int:
    return int(os.environ.get(name) or default)


@dataclasses.dataclass(frozen=True)
class Config:
    """One tick's settings, already validated and already corrected."""

    repo: str
    pool: str
    attr_prefix: str
    region: str
    secret_name: str
    pool_size: int
    max_replacements: int
    concurrency: int
    # -- autoscaling --
    min_warm: int
    max_online: int
    headroom: int
    idle_grace: float
    max_stops: int
    runner_label: str
    tick_mode: str
    # Rotated per run so one permanently-broken low-numbered member cannot
    # own the whole replacement budget forever.
    run_number: int
    # What was misconfigured. Non-empty means the run reports failure, but
    # the values above are already the safe reading, so it still reconciles.
    refusals: tuple[str, ...] = ()

    @property
    def autoscaling(self) -> bool:
        """Can demand move the answer at all?

        When the floor meets the ceiling the clamp pins desired to
        max_online whatever the queue says, so there is nothing to ask
        GitHub. That is the default, and it is why an unconfigured pool
        never pays for a scan and never needs a label.
        """
        return self.min_warm < self.max_online

    @property
    def may_scale_down(self) -> bool:
        """Only a scheduled tick may switch machines off."""
        return self.tick_mode == "scheduled"

    def member_name(self, member: int) -> str:
        return f"{self.pool}-runner-{member}"

    @classmethod
    def from_env(cls) -> "Config":
        """Read the whole knob surface, then validate it as a whole.

        Validation is one pass at the end because the rules are RELATIONS -
        a min_warm is only wrong relative to a max_online and a pool_size -
        and checking a relation while its other side is still unread is how
        a validator ends up disagreeing with itself.
        """
        pool = pool_name()
        pool_size = _int("POOL_SIZE", 8)
        # MIN_WARM defaults to the whole pool: unset, every member stays on
        # and this is exactly the pre-autoscaling reconcile.
        min_warm = _int("MIN_WARM", pool_size)
        max_online = _int("MAX_ONLINE", pool_size)
        refusals: list[str] = []

        if not 0 <= min_warm <= max_online <= pool_size:
            refusals.append(
                f"MIN_WARM={min_warm}, MAX_ONLINE={max_online} and"
                f" POOL_SIZE={pool_size} must satisfy"
                " 0 <= MIN_WARM <= MAX_ONLINE <= POOL_SIZE"
            )

        runner_label = os.environ.get("RUNNER_LABEL") or ""
        # The workflow's own token, which needs `actions: read` and nothing
        # else. Falling back to the PAT would work only if someone had
        # granted it the Actions permission, and the whole point is that it
        # must not have one.
        demand_token = os.environ.get("GITHUB_TOKEN") or ""
        if not refusals and min_warm < max_online:
            if not runner_label:
                refusals.append(
                    "RUNNER_LABEL is unset, so there is no demand signal. Set"
                    " it to the label your jobs target (one of"
                    " `services.ix-runner.labels`). It is what a bootstrap"
                    " pool matches jobs against before any runner has"
                    " registered a label set of its own; without it a pool"
                    " with nothing registered can serve no job it can see."
                )
            elif not demand_token:
                refusals.append(
                    "GITHUB_TOKEN is unset, so the job queue cannot be read."
                    " Pass the workflow's own token (github-token on the"
                    " action) and give the job `permissions: actions: read`."
                )

        if refusals:
            # Off means the WHOLE pool stays on. Leaving a hand-set
            # MAX_ONLINE in place would keep stopping machines while the run
            # announced that scaling was off.
            for why in refusals:
                log_error(f"autoscaling is off for this run: {why}")
            min_warm = max_online = pool_size

        return cls(
            repo=os.environ["GITHUB_REPOSITORY"],
            pool=pool,
            attr_prefix=attr_prefix(),
            region=os.environ.get("IX_REGION") or "us-west-1",
            secret_name=os.environ.get("SECRET_NAME") or f"{pool}_runner_reg_token",
            pool_size=pool_size,
            max_replacements=_int("MAX_REPLACEMENTS", 2),
            concurrency=_int("CONCURRENCY", 4),
            min_warm=min_warm,
            max_online=max_online,
            headroom=_int("SCALE_HEADROOM", 2),
            # Seconds, not ticks: idle time is derived from GitHub's own job
            # timestamps, so it is a real duration and does not depend on
            # how often this runs. A tick counter meant the grace silently
            # changed length whenever the cron did.
            idle_grace=float(os.environ.get("IDLE_GRACE_SECONDS") or 600),
            # Only stops are capped. Being SHORT of capacity is the state
            # with a queue behind it, so a start is never rationed; being
            # long of it costs money, which can wait for the next tick.
            max_stops=_int("MAX_STOPS", 4),
            runner_label=runner_label,
            # Which trigger this is. Only a scheduled tick may switch
            # machines OFF: an event tick fires when a run is REQUESTED,
            # before its jobs reach the queue, so it sees an idle pool at
            # the exact moment a wave is landing.
            tick_mode=(
                os.environ.get("TICK_MODE")
                or (
                    "scheduled"
                    if os.environ.get("GITHUB_EVENT_NAME") in SCALE_DOWN_EVENTS
                    else "event"
                )
            ).lower(),
            run_number=_int("GITHUB_RUN_NUMBER", 0),
            refusals=tuple(refusals),
        )
