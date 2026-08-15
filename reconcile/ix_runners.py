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
import os
import sys
import time
from typing import Any

from .config import Config, desired_rev
from .executor import execute, mint_token, needs_token
from .github import list_runners
from .machines import client
from .planner import plan
from .report import log_error, log_warning, write_summary
from .snapshot import observe_pool


async def reconcile(ix: Any) -> int:
    """Converge the pool; return the number of creations/replacements.

    Four steps, and they are the same four the docstring above describes:
    look at the world, decide from that snapshot alone, apply the decision,
    then say what happened. Each is somebody else's module - the value of
    keeping this function short is that the ORDER is the only thing it
    expresses, and the order is the part that has bitten us.
    """
    pat = os.environ["RUNNER_PAT"]
    # Mask the admin PAT for the whole run, as we do the registration token: it
    # is never deliberately printed, but this redacts it from any traceback the
    # runner emits. flush so the directive lands before anything it must cover.
    print(f"::add-mask::{pat}", flush=True)
    demand_token = os.environ.get("GITHUB_TOKEN") or ""
    config = Config.load()
    # A refused knob is a red run, but not a dead one: the values are already
    # the safe reading, so the pool still gets healed.
    failures = len(config.refusals)

    # -- observe --
    rev = desired_rev()
    runners = list_runners(pat, config.repo)
    vms = {info.name: info for info in await ix.machines().list()}
    snapshot = await observe_pool(
        ix, config, demand_token, rev=rev, runners=runners, vms=vms
    )

    # -- decide -- (pure: no I/O below this line until execute)
    decision = plan(snapshot, config, time.time())
    for level, message in decision.notes:
        if level == "warn":
            log_warning(message)
        else:
            print(message)

    # -- execute --
    if needs_token(decision.actions):
        await mint_token(ix, config, pat)
    outcome = await execute(
        ix,
        config,
        pat,
        decision.actions,
        vms=vms,
        runners=runners,
        rev=rev,
        summary=list(decision.summary),
        applied=decision.admitted,
    )
    failures += outcome.failures

    # -- summarize --
    # The spent token is deliberately LEFT in the secret store. It is dead
    # within the hour and can do nothing but register a runner on this one
    # repo, and keeping the row is what makes the next write a rotation
    # rather than an insert - which is the only way a stopped machine ever
    # receives a usable token again.
    print(
        f"reconcile done: {outcome.applied} creation(s)/replacement(s),"
        f" {outcome.power_changes} power change(s), {failures} failed"
    )
    write_summary(sorted(outcome.summary))
    if failures:
        raise SystemExit(1)
    return outcome.applied

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
