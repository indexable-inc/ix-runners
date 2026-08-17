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
import pathlib
import subprocess
import tomllib

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


# The pool spec's whole vocabulary. Anything else in the file is a typo, and
# a typo that silently defaults is a pool quietly running someone else's
# numbers - `mniWarm` would read as "autoscaling off" and cost real money.
# Same reasoning as a deny-unknown-fields deserializer.
SPEC_KEYS = {
    "pool-name": str,
    "region": str,
    # Alternative to `region`: a list the pool is spread across, member
    # index modulo the list, so capacity and blast radius split evenly.
    # One region's host trouble then takes a fraction of the pool, and a
    # create that fails in a member's home region retries once in the next
    # one the same tick. Set one of `region`/`regions`, never both.
    "regions": list,
    "attr-prefix": str,
    "runner-label": str,
    "pool-size": int,
    "min-warm": int,
    "max-online": int,
    "scale-headroom": int,
    "idle-grace-seconds": int,
    "max-stops": int,
    "max-replacements": int,
    "concurrency": int,
}

# Where the spec lives unless the action is told otherwise. Under nix/ on
# purpose: that directory is already one of CONFIG_PATHS, so a change to the
# pool's shape rolls the fleet the same way any other config change does.
#
# TOML rather than JSON because this file is read far more often than it is
# written, and every key in it wants a line saying what it does - which JSON
# has nowhere to put. tomllib has been stdlib since 3.11 and the entrypoint
# pins >=3.13, so reading it adds no dependency to a job holding a repo-admin
# PAT. TOML also keeps its types honest: an int stays an int and a bool stays
# a bool, which is what the checks below rely on.
DEFAULT_SPEC_PATH = "nix/ix-pool.toml"


def load_spec(path: str) -> dict[str, object]:
    """Read the pool spec, refusing anything it does not understand."""
    file = pathlib.Path(path)
    if not file.is_file():
        log_error(
            f"no pool spec at {path}. This file is the pool's definition and"
            " both sides read it - the flake builds the members from it and"
            " this reconcile manages them from it. A minimal one is three"
            ' lines: pool-name = "<name>" / region = "<region>" /'
            " pool-size = 8."
        )
        raise SystemExit(1)
    try:
        # Binary, because tomllib insists on it: TOML is defined as UTF-8 and
        # it decodes rather than trusting the platform's locale.
        with file.open("rb") as handle:
            spec = tomllib.load(handle)
    except (OSError, ValueError) as error:
        log_error(f"{path} could not be read as TOML: {error}")
        raise SystemExit(1) from error

    problems = []
    for key, value in spec.items():
        want = SPEC_KEYS.get(key)
        if want is None:
            near = [known for known in SPEC_KEYS if known.replace("-", "") == str(key).replace("-", "").lower()]
            hint = f" (did you mean {near[0]!r}?)" if near else ""
            problems.append(f"unknown key {key!r}{hint}")
        # bool is an int subclass, and `pool-size = true` is not a size.
        # TOML parses a real bool here rather than coercing, so this check is
        # doing exactly as much work as it was.
        elif want is int and (isinstance(value, bool) or not isinstance(value, int)):
            problems.append(f"{key!r} must be a whole number, got {value!r}")
        elif want is str and not isinstance(value, str):
            problems.append(f"{key!r} must be a string, got {value!r}")
        elif want is list and (
            not isinstance(value, list)
            or not value
            or not all(isinstance(item, str) and item for item in value)
        ):
            problems.append(
                f"{key!r} must be a non-empty list of region strings, got {value!r}"
            )
    if problems:
        for problem in problems:
            log_error(f"{path}: {problem}")
        log_error(f"{path}: known keys are {', '.join(sorted(SPEC_KEYS))}")
        raise SystemExit(1)
    return spec


def spec_from_env() -> dict[str, object]:
    """The same shape, assembled from the environment.

    Every value the action used to pass as its own input. Kept because the
    ambient half of the configuration (which repo, which run, which event)
    can only come from the environment anyway, and because it lets the test
    suite drive a Config without writing a file for every case. Production
    reads the spec file; this is what fills the gaps around it.
    """
    spec: dict[str, object] = {}
    for key, want in SPEC_KEYS.items():
        raw = os.environ.get(key.upper().replace("-", "_"))
        if raw:
            if want is int:
                spec[key] = int(raw)
            elif want is list:
                # REGIONS="us-west-1,us-east-1" - same comma convention as
                # every other list-shaped CI environment variable.
                spec[key] = [part.strip() for part in raw.split(",") if part.strip()]
            else:
                spec[key] = raw
    return spec


def _int(name: str, default: int) -> int:
    return int(os.environ.get(name) or default)


@dataclasses.dataclass(frozen=True)
class Config:
    """One tick's settings, already validated and already corrected."""

    repo: str
    pool: str
    attr_prefix: str
    # Every region this pool places members in, in spec order. One entry is
    # the classic single-region pool; more spread members index-modulo
    # across the list. A tuple because a Config is one tick's immutable
    # reading, and because member->region must be a pure function of the
    # spec - a set would let iteration order reassign the whole pool.
    regions: tuple[str, ...]
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

    def region_for(self, member: int) -> str:
        """A member's home region: index modulo the list.

        Deterministic on purpose - a member REPLACES in the region it lived
        in, so warm state (image cache, learned floors) stays meaningful,
        and the split stays even without any bookkeeping. Changing the list
        reassigns homes, which takes effect member by member as replacements
        happen; the reconcile never migrates a healthy member.
        """
        return self.regions[member % len(self.regions)]

    def failover_region(self, home: str) -> str | None:
        """Where a failed create retries, or None for a single-region pool.

        The next region in spec order. One alternative, not a tour of the
        whole list: a create that fails everywhere should fail the member
        loudly rather than time out the tick trying coasts in sequence.
        """
        if len(self.regions) < 2:
            return None
        return self.regions[(self.regions.index(home) + 1) % len(self.regions)]

    @classmethod
    def load(cls) -> "Config":
        """The one entry point: the spec file when there is one, else env.

        IX_POOL_SPEC is set by the composite action and points at the
        repo's pool spec. Without it - which in practice means the test
        suite - the same shape is assembled from the environment, so there
        is exactly ONE implementation of every default and every rule
        below, whichever way the values arrived.
        """
        path = os.environ.get("IX_POOL_SPEC")
        return cls.from_spec(load_spec(path) if path else spec_from_env())

    @classmethod
    def from_spec(cls, spec: dict[str, object]) -> "Config":
        """Fill in the defaults, then validate the whole thing at once.

        Validation is one pass at the end because the rules are RELATIONS -
        a min_warm is only wrong relative to a max_online and a pool_size -
        and checking a relation while its other side is still unread is how
        a validator ends up disagreeing with itself.
        """

        def number(key: str, default: int) -> int:
            value = spec.get(key)
            return default if value is None else int(value)  # type: ignore[arg-type]

        def text(key: str, default: str) -> str:
            value = spec.get(key)
            return default if value is None else str(value)

        pool = text("pool-name", pool_name())
        # POOL_SIZE x `slots` runner daemons each = the concurrent job budget.
        # The flake's mkPool reads this SAME key, which is what stopped the
        # two from drifting: a larger pool here used to ask for flake attrs
        # that did not exist, and every run was red until someone noticed.
        pool_size = number("pool-size", 8)
        # min-warm defaults to the whole pool: unset, every member stays on
        # and this is exactly the pre-autoscaling reconcile.
        min_warm = number("min-warm", pool_size)
        max_online = number("max-online", pool_size)
        refusals: list[str] = []

        if not 0 <= min_warm <= max_online <= pool_size:
            refusals.append(
                f"min-warm={min_warm}, max-online={max_online} and"
                f" pool-size={pool_size} must satisfy"
                " 0 <= min-warm <= max-online <= pool-size"
            )

        runner_label = text("runner-label", "")
        # The workflow's own token, which needs `actions: read` and nothing
        # else. Falling back to the PAT would work only if someone had
        # granted it the Actions permission, and the whole point is that it
        # must not have one.
        demand_token = os.environ.get("GITHUB_TOKEN") or ""
        if not refusals and min_warm < max_online:
            if not runner_label:
                refusals.append(
                    "runner-label is unset, so there is no demand signal. Set"
                    " it in the pool spec to the label your jobs target (one"
                    " of `services.ix-runner.labels`). It is what a bootstrap"
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
            # max-online in place would keep stopping machines while the run
            # announced that scaling was off.
            for why in refusals:
                log_error(f"autoscaling is off for this run: {why}")
            min_warm = max_online = pool_size

        # `region` and `regions` are one setting with two spellings, and a
        # spec carrying both is a spec whose author believes two different
        # things about where the pool lives - refuse rather than pick.
        if spec.get("region") is not None and spec.get("regions") is not None:
            log_error(
                "the pool spec sets both `region` and `regions`; set exactly"
                " one (`regions` with a single entry is the same pool as"
                " `region`)"
            )
            raise SystemExit(1)
        regions_value = spec.get("regions")
        if regions_value is not None:
            regions = tuple(str(item) for item in regions_value)  # type: ignore[union-attr]
            if len(set(regions)) != len(regions):
                log_error(
                    f"`regions` repeats an entry ({list(regions)!r}); each"
                    " region appears once - the SHARE of the pool it hosts"
                    " is its position count, and a duplicate is almost"
                    " certainly a typo, not a weighting scheme"
                )
                raise SystemExit(1)
        else:
            regions = (text("region", "us-west-1"),)

        return cls(
            repo=os.environ["GITHUB_REPOSITORY"],
            pool=pool,
            attr_prefix=text("attr-prefix", "ci-runner"),
            regions=regions,
            secret_name=os.environ.get("SECRET_NAME") or f"{pool}_runner_reg_token",
            pool_size=pool_size,
            max_replacements=number("max-replacements", 2),
            concurrency=number("concurrency", 4),
            min_warm=min_warm,
            max_online=max_online,
            headroom=number("scale-headroom", 2),
            # Seconds, not ticks: idle time is derived from GitHub's own job
            # timestamps, so it is a real duration and does not depend on
            # how often this runs. A tick counter meant the grace silently
            # changed length whenever the cron did.
            idle_grace=float(number("idle-grace-seconds", 600)),
            # Only stops are capped. Being SHORT of capacity is the state
            # with a queue behind it, so a start is never rationed; being
            # long of it costs money, which can wait for the next tick.
            max_stops=number("max-stops", 4),
            runner_label=runner_label,
            # Which trigger this is. NOT a spec key: the trigger already
            # says, and an operator pinning it in a file would be pinning it
            # for the cron too. TICK_MODE exists for tests.
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
