"""The whole decision, as a pure function of what was observed.

`plan` reads a PoolSnapshot and a Config and returns a Plan. It performs no
I/O, opens no socket, touches no guest, and does not read the clock - `now`
is handed in. That is the point: every rule about when a machine may be
deleted or switched off is in one place that a test can drive directly, with
no fakes to stand up and no ordering to arrange.

Nothing here prints, either. Notes travel with the plan and the caller emits
them, so the log a run produces is a rendering of the decision rather than a
side effect scattered through the deciding.

Two phases, in order, because the second depends on the first: HEAL decides
what is broken (missing, dead, stale, offline) and books it, then SCALE sizes
what is left against the queue. A member already booked for a replace is not
capacity the scaler can count on, and one being repaired is.
"""

from __future__ import annotations

from .config import Config
from .machines import BOOT_GRACE
from .model import Action, Plan, PoolSnapshot, WARM_GRACE
from .report import clean

# How old a member may get while its stale-rev replacement keeps deferring
# before the run says so out loud. Generous: a healthy pool replaces members
# far sooner, so reaching this at all means something never converges.
MAX_LIFETIME = 30 * 24 * 60 * 60


class _Notes:
    """Collects what the run should say, in the order it was decided."""

    def __init__(self) -> None:
        self.lines: list[tuple[str, str]] = []

    def say(self, message: str) -> None:
        self.lines.append(("", message))

    def warn(self, message: str) -> None:
        self.lines.append(("warn", message))


def heal_order(config: Config) -> list[int]:
    """Members in the order this run should consider them.

    Rotated by run number: with a fixed order, one permanently-broken
    low-numbered member owns the whole budget run after run and nothing
    above it ever converges.
    """
    size = config.pool_size
    start = config.run_number % size if size else 0
    return [(start + offset) % size + 1 for offset in range(size)]


def plan(snapshot: PoolSnapshot, config: Config, now: float) -> Plan:
    """Decide everything this tick will do. No I/O, no clock, no output."""
    notes = _Notes()
    actions: list[Action] = []
    summary: list[tuple[str, str, str]] = []
    admitted = 0
    budget = config.max_replacements

    # Empty pool = first bootstrap: nothing exists to thrash, so the cap
    # protects nothing - raise it and build the whole pool in one run.
    if not any(member.exists for member in snapshot.members):
        notes.say(f"empty pool -> bootstrap: raising the cap to {config.pool_size}")
        budget = max(budget, config.pool_size)

    def admit(kind: str, index: int, name: str) -> None:
        # Budget is spent at ADMISSION: a bad template rev stalls after N
        # attempts even when every attempt fails.
        nonlocal admitted
        if admitted >= budget:
            notes.say(
                f"{name}: replacement budget ({budget}) exhausted;"
                " reconciles next run"
            )
            summary.append((name, kind, "deferred (budget)"))
            return
        admitted += 1
        actions.append(Action(kind, index, name))

    # -- heal --
    running: list[int] = []  # powered on, whatever its health
    online: list[int] = []  # running, with a runner registered online
    warming: list[int] = []  # running and young, runner not registered yet
    stopped: list[int] = []  # parked; startable in seconds

    members = snapshot.by_index()
    for index in heal_order(config):
        member = members[index]
        name = member.name
        if not member.exists:
            notes.say(f"{name}: missing -> create")
            admit("create", index, name)
            continue
        if member.status == "failed":
            # A platform verdict, not a slow boot: BOOT_GRACE exists because
            # a young machine's silence says nothing, and this machine is not
            # silent - it has told us it is dead. Waiting out the grace on it
            # is 30 minutes of a pool member that will never come back.
            notes.warn(
                f"{name}: the platform reports it FAILED"
                f" ({clean(getattr(member.info, 'failure_reason', None))}) -> replace"
            )
            admit("replace", index, name)
            continue
        if member.status == "stopped":
            # Parked by autoscaling (or by hand). Its disk - and with it the
            # runner credentials - is intact; the scaling plan below decides
            # whether to wake it.
            notes.say(f"{name}: stopped")
            stopped.append(index)
            continue
        if member.status != "running":
            # Not a status this version understands. Every branch below reads
            # a silent guest as "delete and rebuild", and a status we cannot
            # interpret is no evidence at all that the machine is dead.
            notes.warn(f"{name}: unknown status {clean(member.status or 'none')} -> skip")
            summary.append((name, "skip", f"status {clean(member.status or 'none')}"))
            continue
        running.append(index)
        if member.rev is None:
            # A machine started moments ago answers nothing yet, and its AGE
            # says nothing about that: age is measured from creation, so a
            # member created last week and started twenty seconds ago sails
            # past BOOT_GRACE and gets deleted - taking with it the disk, and
            # the registration credentials that make a stop cheap in the
            # first place. Autoscaling creates this case on every scale-up,
            # and a wake-triggered reconcile lands right in it.
            if member.warming:
                notes.say(
                    f"{name}: started {int(WARM_GRACE)}s-fresh, guest not up yet"
                    " -> warming"
                )
                warming.append(index)
                summary.append((name, "skip", "warming"))
                continue
            if member.age is not None and member.age < BOOT_GRACE:
                notes.say(f"{name}: {int(member.age)}s old, still building/booting -> skip")
                summary.append((name, "skip", "booting"))
                continue
            notes.warn(
                f"{name}: unreachable (status {clean(member.status or 'unknown')},"
                f" failure {clean(getattr(member.info, 'failure_reason', None))})"
                " -> replace"
            )
            admit("replace", index, name)
            continue
        if member.rev != snapshot.rev:
            # Never roll a member out from under a running job: config
            # rolls wait for idleness, this member converges on a later run.
            if member.busy:
                # ...unless it is never idle at scan time, in which case it
                # defers its own replacement forever and no runner-config
                # change - including a security one - ever reaches it. Say
                # so; do NOT force the replace. The execute-time deregister
                # refuses a busy member from the same snapshot, and bypassing
                # that check is what leaves a member half-deregistered and
                # serving at reduced capacity. A real drain (disable the
                # registrations, wait for idle, then replace) is the fix, and
                # it is a bigger change than this pass.
                if member.age is not None and member.age > MAX_LIFETIME:
                    notes.warn(
                        f"{name}: {int(member.age // 86400)} days old, on a stale rev,"
                        " and busy at every scan, so its replacement keeps"
                        " deferring and the current runner config has never"
                        " reached it. Drain it by hand: disable its runners in"
                        " the repo's Actions settings, let the jobs finish, and"
                        " re-run this workflow."
                    )
                notes.say(f"{name}: stale rev but busy -> deferred")
                summary.append((name, "replace", "deferred (busy)"))
                continue
            # The rev is whatever the guest printed, so it is cleaned and
            # truncated before it reaches a log line.
            notes.say(
                f"{name}: rev {clean(member.rev[:12])} != {snapshot.rev[:12]} -> replace"
            )
            admit("replace", index, name)
            continue
        if member.online:
            notes.say(f"{name}: healthy")
            online.append(index)
            continue
        if member.warming:
            # Started within WARM_GRACE, so its runners are still coming up.
            # Repairing it here would restart units mid-registration, and the
            # scaling plan needs it counted as on-its-way-online or the next
            # tick starts a second machine for the same job.
            notes.say(f"{name}: warming (started recently, runners not up yet)")
            warming.append(index)
            summary.append((name, "skip", "warming"))
            continue
        # Offline but reachable and on the right rev: repair once by
        # restarting the units (a configured runner re-registers from its
        # persisted state and needs no fresh token); replace only if a prior
        # run already repaired and it is STILL offline (two-strike, with the
        # strike recorded on the VM itself so this script stays stateless).
        if member.struck:
            notes.say(f"{name}: still offline after repair -> replace")
            admit("replace", index, name)
            continue
        notes.say(f"{name}: runners offline -> repair (restart units)")
        actions.append(Action("repair", index, name))

    # A shrunk POOL_SIZE orphans the members above it: they keep billing and
    # keep taking jobs from a config nobody reconciles. Prune them on budget.
    for index in snapshot.extra:
        name = config.member_name(index)
        notes.warn(f"{name}: above POOL_SIZE ({config.pool_size}) -> deregister and delete")
        admit("prune", index, name)

    # -- scale --
    # Level-based, and stateless by construction: every number below was
    # observed fresh this tick. Nothing is remembered between runs, so two
    # reconciles cannot disagree about what the pool looked like, and a
    # missed tick costs latency rather than correctness.
    #
    # A member already booked for a create/replace/repair is not a power
    # candidate: stopping something mid-replace, or starting something whose
    # units are being restarted, races the action for no benefit.
    booked = {action.member for action in actions}
    # Everything already powered on counts as capacity, not just the healthy.
    # A warming member is seconds from taking a job; one being repaired, or
    # deferred mid-config-roll, is up and serving right now. Members booked
    # for a replace or a prune do NOT count: they are about to go away.
    doomed = {a.member for a in actions if a.kind in ("replace", "prune")}
    effective = len([index for index in running if index not in doomed])

    seen = snapshot.observation
    if not config.autoscaling:
        # The clamp pins desired to max_online whatever demand says, so there
        # is nothing to ask GitHub. This is the default path: every member
        # that exists should be on, and one stopped by hand is STARTED rather
        # than deleted and rebuilt.
        desired = config.max_online
        why = "autoscaling off"
    elif seen is None:
        # A tick that learned nothing makes NO scaling decision. Missing
        # data is not zero demand, and it is not zero idleness either:
        # guessing up strands nothing but costs money forever, guessing
        # down stops machines about to be handed a job. Healing above
        # still ran; only scaling is skipped.
        desired = effective
        why = "queue unreadable"
    else:
        # Jobs round UP into members: one leftover job still needs a
        # whole machine to run on.
        needed = -(-seen.demand // snapshot.slots)
        desired = min(max(needed + config.headroom, config.min_warm), config.max_online)
        why = (
            f"{seen.demand} servable job(s)/{snapshot.slots} slot(s) = {needed}"
            f" +{config.headroom} headroom,"
            f" clamped [{config.min_warm},{config.max_online}]"
        )

    starts: list[int] = []
    stops: list[int] = []
    if effective < desired:
        # Start before create, always: a stopped member is a boot, a missing
        # one is a template build. Lowest index first, the mirror of the stop
        # order, so the warm core is a stable set of the same machines.
        # Deliberately NOT rate-capped: being short of capacity is the state
        # with a queue behind it, and every tick of delay is queued jobs.
        starts = sorted(index for index in stopped if index not in booked)[
            : desired - effective
        ]
    elif effective > desired and seen is not None:
        if not config.may_scale_down:
            # An event tick fires when a run is REQUESTED, which is the
            # moment before its jobs appear in the queue: the pool looks idle
            # precisely because the wave has not landed yet. Scaling down
            # here would switch machines off at the start of a wave. Event
            # ticks may only ever add capacity.
            notes.say(
                f"{effective - desired} surplus member(s), but an event tick"
                " never scales down"
            )
        else:
            surplus = effective - desired
            # Never a busy one, and never a warming one (it has not had the
            # chance to take a job yet, and stopping it wastes the boot).
            # Highest index first: the low members stay warm, so their
            # template and toolchain caches stay hot.
            for index in sorted(online, reverse=True):
                if len(stops) >= min(surplus, config.max_stops):
                    break
                member = members[index]
                if index in booked or member.busy:
                    continue
                # Idle time is DERIVED: the last completion GitHub recorded
                # for any of this member's runners. No stored counter, no
                # consecutive-tick bookkeeping, nothing to get out of step.
                idle_for = seen.idle_for(list(member.runner_names), now)
                if idle_for is None:
                    # The scan saw no completions at all, so "never finished a
                    # job recently" is indistinguishable from "the window is
                    # empty". Not evidence of idleness.
                    notes.say(f"{member.name}: no idle history in the scan -> not stopped")
                    continue
                if idle_for < config.idle_grace:
                    # Covers both shapes at once, which is why there is no
                    # separate window check: for a member the scan DID see
                    # finish, this is its real idle time; for one it did not,
                    # idle_for is the window's own length, so a window
                    # shorter than the grace fails here exactly as it should.
                    # Absence from a short window is not evidence of idleness.
                    notes.say(
                        f"{member.name}: idle {int(idle_for)}s of"
                        f" {int(config.idle_grace)}s grace -> not yet"
                    )
                    continue
                stops.append(index)

    for index in starts:
        actions.append(Action("start", index, config.member_name(index)))
    for index in stops:
        actions.append(Action("stop", index, config.member_name(index)))

    # One line per tick, so a reader can reconstruct the decision without
    # replaying the log: what was observed, what it implied, what was done.
    notes.say(
        f"DECISION [{config.tick_mode}] powered_on={effective}"
        f" (online={len(online)} warming={len(warming)} stopped={len(stopped)})"
        f" demand={seen.demand if seen else 'n/a'}"
        f"{' (truncated)' if seen and seen.truncated else ''}"
        f" -> desired={desired} [{why}]"
        f" | start {sorted(starts)} stop {sorted(stops)}"
    )

    return Plan(
        actions=tuple(actions),
        notes=tuple(notes.lines),
        summary=tuple(summary),
        admitted=admitted,
    )
