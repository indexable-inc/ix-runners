"""Look at the world once, and turn it into a value the planner can read.

Everything that asks a question - GitHub's runner listing, the machine rows,
a guest probe per member, the job queue - happens here and only here, so the
decision that follows is a function of data rather than of whatever the
network happened to answer partway through deciding.

Probes run CONCURRENTLY and then all decisions are taken from that one
snapshot. Probing sequentially made an all-unreachable pool pay EXEC_TIMEOUT
per member before anything happened at all.
"""

from __future__ import annotations

from typing import Any

from .config import Config
from .github import (
    IDLE_SCAN_RUNS,
    extra_members,
    member_busy,
    member_online,
    member_runners,
    observe_or_none,
    pool_slots,
    runner_label_sets,
)
from .machines import probe_member
from .model import Member, PoolSnapshot, machine_age, machine_status, started_recently
from .report import log_warning

import asyncio


def label_sets_for(
    runners: list[dict[str, Any]], config: Config
) -> list[set[str]]:
    """What this pool can serve, preferring what its runners actually say.

    Read off the registrations so GitHub's matching rule is applied against
    real advertised labels. A pool with nothing registered yet (a bootstrap)
    has none to read, so fall back to the declared label plus the implicit
    self-hosted one.
    """
    return runner_label_sets(runners, config.pool, config.pool_size) or (
        [{"self-hosted", config.runner_label}] if config.runner_label else []
    )


async def probe_all(
    ix: Any,
    config: Config,
    vms: dict[str, Any],
    runners: list[dict[str, Any]],
) -> dict[int, tuple[str | None, bool]]:
    """Ask every running member what rev it is on. One round trip each."""
    gate = asyncio.Semaphore(config.concurrency)
    members = list(range(1, config.pool_size + 1))

    async def probe(member: int) -> tuple[int, str | None, bool]:
        info = vms.get(config.member_name(member))
        # A stopped or failed machine has no guest to answer, and the planner
        # reads silence as "unreachable -> replace". Probing one would spend
        # EXEC_TIMEOUT to learn nothing and then delete every machine
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
                    machine,
                    clear_marker=member_online(runners, config.pool, member),
                )
            except Exception as error:
                log_warning(
                    f"{config.member_name(member)}: probe failed ({error!r})"
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
                f"{config.member_name(member)}: probe raised past the handler"
                f" ({outcome!r}) -> reading it as unreachable"
            )
            state[member] = (None, False)
            continue
        _, found, struck = outcome
        state[member] = (found, struck)
    return state


async def observe_pool(
    ix: Any,
    config: Config,
    demand_token: str,
    *,
    rev: str,
    runners: list[dict[str, Any]],
    vms: dict[str, Any],
) -> PoolSnapshot:
    """Everything one tick knows, collapsed into one value."""
    state = await probe_all(ix, config, vms, runners)
    members = []
    for index in range(1, config.pool_size + 1):
        name = config.member_name(index)
        info = vms.get(name)
        found, struck = state[index]
        members.append(
            Member(
                index=index,
                name=name,
                info=info,
                status=machine_status(info) if info is not None else "",
                rev=found,
                struck=struck,
                online=member_online(runners, config.pool, index),
                busy=member_busy(runners, config.pool, index),
                runner_names=tuple(
                    runner["name"] for runner in member_runners(runners, config.pool, index)
                ),
                age=machine_age(info) if info is not None else None,
                warming=started_recently(info) if info is not None else False,
            )
        )

    # Only ASK when the answer could move the decision. With the floor at the
    # ceiling the clamp pins desired to max_online whatever the queue says,
    # so an unconfigured pool never pays for a scan and never needs a label.
    observation = None
    if config.autoscaling:
        observation = observe_or_none(
            demand_token, config.repo, label_sets_for(runners, config), IDLE_SCAN_RUNS
        )

    return PoolSnapshot(
        members=tuple(members),
        extra=tuple(extra_members(list(vms), config.pool, config.pool_size)),
        rev=rev,
        slots=pool_slots(runners, config.pool, config.pool_size),
        observation=observation,
    )
