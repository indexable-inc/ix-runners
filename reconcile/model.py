"""The typed shapes the rest of the reconcile passes around.

A Member is one pool slot as this tick found it - machine row, probe answer
and GitHub registration state collapsed into one record - and a PoolSnapshot
is the whole observed world. Everything here is data: no I/O, no clock reads
except the ones handed a timestamp, nothing that can fail.
"""

from __future__ import annotations

import dataclasses
import time
from typing import Any

# A machine started this recently is WARMING: the platform reports Running
# the moment it is coming up (MachineStatus has no "starting" state), but its
# runner daemons take a few more seconds to register. Without this window a
# machine autoscaling started reads as offline-and-reachable and gets
# "repaired" on the spot, and a second tick starts it all over again.
WARM_GRACE = 300

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

def machine_age(info: Any) -> float | None:
    """Seconds since the machine row was created (the SDK reports epoch ms)."""
    created_ms = getattr(info, "created_at", None)
    if not created_ms:
        return None
    return time.time() - created_ms / 1000


@dataclasses.dataclass(frozen=True)
class Member:
    """One pool slot, as this tick found it.

    Absent fields mean absent facts, not defaults: `info` is None when no
    machine exists at all, and `rev` is None when the guest did not answer -
    which is a different thing from answering with the wrong rev, and the
    two lead to different decisions.
    """

    index: int
    name: str
    # The machine row, or None when the member does not exist yet.
    info: Any | None
    # Lifecycle status, lowercased; "" when there is no machine.
    status: str
    # The rev the guest reported. None = unreachable, or never probed
    # because the machine is not running.
    rev: str | None
    # The two-strike marker was on the VM: a previous run already repaired it.
    struck: bool
    # At least one of its runner daemons is registered and online.
    online: bool
    # At least one of its runner daemons is mid-job.
    busy: bool
    # Names of its runner registrations, which is what the idle clock is
    # keyed by.
    runner_names: tuple[str, ...]
    # Seconds since the machine ROW was created. Not since it started.
    age: float | None
    # Started inside WARM_GRACE, so its runners may simply not be up yet.
    warming: bool

    @property
    def exists(self) -> bool:
        return self.info is not None


@dataclasses.dataclass(frozen=True)
class PoolSnapshot:
    """Everything one tick observed, before any decision is taken."""

    members: tuple[Member, ...]
    # Members above POOL_SIZE: orphans of a shrink, still billing.
    extra: tuple[int, ...]
    # The runner-config rev every member is supposed to be running.
    rev: str
    # Runner daemons per VM, read back off the registrations.
    slots: int
    # What GitHub said about the queue, or None when it could not be read.
    observation: Any | None

    def by_index(self) -> dict[int, Member]:
        return {member.index: member for member in self.members}


@dataclasses.dataclass(frozen=True)
class Action:
    """One thing to do to one member. The planner emits these; only the
    executor is allowed to make any of them true."""

    kind: str  # create | replace | repair | prune | start | stop
    member: int
    name: str


@dataclasses.dataclass(frozen=True)
class Plan:
    """The whole decision, as data.

    Notes and summary rows travel WITH the plan rather than being printed
    where they are decided: that is what lets the planner be a pure function
    of the snapshot, and it means a test can assert on the decision without
    parsing a log.
    """

    actions: tuple[Action, ...]
    # (level, message) - level is "" for plain output, or warn/error.
    notes: tuple[tuple[str, str], ...]
    # (member, action, outcome) rows for the job summary.
    summary: tuple[tuple[str, str, str], ...]
    # Creations + replacements admitted, which is what the budget counts and
    # what the run reports.
    admitted: int
