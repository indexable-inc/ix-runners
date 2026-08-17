"""Everything that changes the world. Nothing here decides anything.

The planner already said what should happen; this makes it happen and
reports what actually did, which are not the same list - a stop can find its
member busy, a create can fail. Two properties are load-bearing and easy to
lose:

  * ONE member's failure is that member's failure. It is logged, its budget
    stays spent, and the loop moves on; the pool converges across runs. An
    exception escaping a gather used to cancel every sibling mid-create.
  * STARTS finish before any stop begins, so a run that dies halfway leaves
    the pool larger than intended, never smaller.
"""

from __future__ import annotations

import asyncio
import dataclasses
from typing import Any

from . import github
from .config import Config
from .machines import REPAIRED_MARKER, create, guest
from .model import Action
from .report import log_error, log_warning


@dataclasses.dataclass
class Outcome:
    """What the execute phase actually managed to do."""

    # (member, action, outcome) rows for the job summary.
    summary: list[tuple[str, str, str]]
    failures: int
    # Creations + replacements that stuck. Starts from the planner's count
    # and drops for each one that turned out to be deferrable.
    applied: int

    @property
    def power_changes(self) -> int:
        """Counted off what happened, not off what was planned: a stop that
        found its member busy changed no power state."""
        return sum(
            1
            for _, kind, outcome in self.summary
            if kind in ("start", "stop") and outcome in ("started", "stopped")
        )


async def mint_token(ix: Any, config: Config, pat: str) -> None:
    """Put a fresh registration token where first boot will find it.

    OVERWRITES the account secret; never delete-then-recreate. The platform
    propagates a rotation to every machine already holding a copy - stopped
    ones included, where it lands in the machine's secret row and is
    delivered at next boot - but ONLY when the write updates an existing
    row. A write that INSERTS is a first write, fires no rotation, and every
    pool member would go on booting with the spent token from whenever it
    was created. Deleting the spent secret at the end of a run, which is
    what this used to do, turns every subsequent write into exactly that
    insert.
    """
    # Module-qualified on purpose: binding these at import time would make
    # the name here a private copy that a test patching `reconcile.github`
    # can never reach, and the fake would be quietly bypassed.
    token = github.github_api(
        pat, config.repo, "/actions/runners/registration-token", method="POST"
    )["token"]
    # Mask BEFORE the token can reach any other output: for its one-hour
    # life it can register a runner that steals this repo's jobs. flush is
    # load-bearing - stdout to a pipe block-buffers, so an unflushed mask
    # can still be sitting in this process while the SDK call below writes
    # the token into a traceback on stderr.
    print(f"::add-mask::{token}", flush=True)
    await ix.secrets().set(config.secret_name, token)


def needs_token(actions: tuple[Action, ...]) -> bool:
    """A create, a replace - or a START.

    Scale-down deregisters, so a woken machine has no registration left: it
    must re-register at boot, and it can only do that with a token it has
    never seen before (the runner re-registers precisely BECAUSE the token
    file changed).
    """
    return any(action.kind in ("create", "replace", "start") for action in actions)


async def execute(
    ix: Any,
    config: Config,
    pat: str,
    actions: tuple[Action, ...],
    *,
    vms: dict[str, Any],
    runners: list[dict[str, Any]],
    rev: str,
    summary: list[tuple[str, str, str]],
    applied: int,
) -> Outcome:
    """Apply the plan. Returns what stuck, never raises for one member."""
    result = Outcome(summary=summary, failures=0, applied=applied)
    if not actions:
        return result

    gate = asyncio.Semaphore(config.concurrency)
    print(f"executing {len(actions)} action(s), concurrency {config.concurrency}")
    # Deregistrations are serialized across members: concurrent DELETEs trip
    # GitHub's secondary rate limit, whose 422 is indistinguishable by status
    # code from a busy runner's refusal.
    deregistering = asyncio.Lock()

    async def deregister(member: int) -> bool:
        # Off the event loop: the blocking urllib calls would otherwise stall
        # every sibling create's timeout budget.
        async with deregistering:
            return await asyncio.to_thread(
                github.deregister_member, pat, config.repo, runners, config.pool, member
            )

    async def run_action(action: Action) -> None:
        kind, member, name = action.kind, action.member, action.name
        async with gate:
            try:
                if kind == "repair":
                    machine = ix.machines().connect(vms[name].id)
                    await guest(machine, "systemctl", "restart", "github-runner-*")
                    await guest(machine, "touch", REPAIRED_MARKER)
                    result.summary.append((name, kind, "units restarted"))
                    return
                if kind == "start":
                    # The token was rotated into the machine's secret row
                    # before this gather began; boot re-reads that row, so
                    # the guest comes up with the FRESH token and its runner
                    # re-registers from scratch. That is the whole reason a
                    # wake costs a token mint: scale-down deregistered it, so
                    # there is nothing left to reconnect with.
                    await ix.machines().connect(vms[name].id).start()
                    result.summary.append((name, kind, "started"))
                    return
                if kind == "stop":
                    # DEREGISTER FIRST, then cut the power. GitHub refuses
                    # (422) to delete a runner that is mid-job, and that
                    # refusal is the only real lock in this system: it is
                    # atomic with respect to job assignment in a way no
                    # amount of re-reading a listing can be. Checking `busy`
                    # and then stopping leaves a window in which a job is
                    # assigned and then killed; deregistering closes it,
                    # because after the DELETE the runner cannot be assigned
                    # anything at all.
                    #
                    # The cost is that the stop is no longer free to undo:
                    # the registration is gone, so waking this member needs a
                    # fresh token. That is what the rotation pays for.
                    try:
                        freed = await deregister(member)
                    except SystemExit:
                        # deregister_member reaches github_api, which exits
                        # the process on an expired PAT. SystemExit is a
                        # BaseException, so return_exceptions on the gather
                        # does NOT hold it - it would tear down every sibling
                        # mid-flight. A stop is the most abandonable action
                        # there is.
                        log_warning(f"{name}: deregister could not run -> left running")
                        result.summary.append((name, kind, "skipped (no listing)"))
                        return
                    if not freed:
                        print(f"{name}: took a job before the stop -> left running")
                        result.summary.append((name, kind, "skipped (busy)"))
                        return
                    await ix.machines().connect(vms[name].id).stop()
                    result.summary.append((name, kind, "stopped"))
                    return
                if kind in ("replace", "prune"):
                    if not await deregister(member):
                        print(f"{name}: picked up a job mid-scan -> deferred")
                        result.summary.append((name, kind, "deferred (busy)"))
                        result.applied -= 1
                        return
                    await ix.machines().connect(vms[name].id).delete()
                if kind != "prune":
                    home = config.region_for(member)
                    try:
                        await create(
                            ix, config.repo, rev, config.secret_name,
                            config.attr_prefix, member, name, home,
                        )
                    except Exception as error:
                        # One retry in the next region, same tick. This is
                        # the multi-region pool's failover: when a region's
                        # hosts are sick (the state that killed members in
                        # the first place), replacements must not pile back
                        # into it just because the modulo says so. Single
                        # region -> no alternative -> the failure propagates
                        # to the per-member handler as before.
                        alt = config.failover_region(home)
                        if alt is None:
                            raise
                        log_warning(
                            f"{name}: create in {home} failed ({error!r});"
                            f" retrying once in {alt}"
                        )
                        await create(
                            ix, config.repo, rev, config.secret_name,
                            config.attr_prefix, member, name, alt,
                        )
                result.summary.append((name, kind, "ok"))
            # Any exception, not just the SDK's: an unforeseen one used to
            # abort the gather and cancel every sibling MID-CREATE.
            except Exception as error:
                result.failures += 1
                log_error(
                    f"{name}: {kind} FAILED ({error!r}); reconciling again next run"
                )
                result.summary.append((name, kind, f"FAILED: {error}"))

    # STARTS (and every healing action) run to completion BEFORE any stop
    # begins, so a tick that dies halfway leaves the pool larger than
    # intended, never smaller: too much capacity is a bill, too little is a
    # stuck queue.
    #
    # As the scaler stands this is a guard rather than a live path - the plan
    # is a single if/elif on effective vs desired, so one tick emits starts
    # or stops and never both (pinned by a test). The split is here so that
    # stops cannot overtake starts if that ever changes.
    phases = [
        [action for action in actions if action.kind != "stop"],
        [action for action in actions if action.kind == "stop"],
    ]
    for phase in phases:
        if not phase:
            continue
        outcomes = await asyncio.gather(
            *(run_action(action) for action in phase), return_exceptions=True
        )
        # run_action swallows Exception itself, so anything surviving is a
        # BaseException; return_exceptions keeps it from cancelling siblings,
        # and it is still a failure.
        for action, outcome in zip(phase, outcomes):
            if isinstance(outcome, BaseException):
                result.failures += 1
                log_error(
                    f"{action.name}: {action.kind} raised past the handler ({outcome!r})"
                )
                result.summary.append((action.name, action.kind, f"FAILED: {outcome!r}"))
    return result
