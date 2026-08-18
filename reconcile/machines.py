"""The ix SDK seam: every call that touches a machine or a secret.

Imports of `ix_sdk` are deliberately inside the functions that need them.
The wheel is x86_64-only, so the test suite - which stands fakes in for all
of this - must be able to import the package on any machine.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

from .report import log_warning

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

def client() -> Any:
    """The ix API client; resolves IX_TOKEN from the environment."""
    from ix_sdk import Client

    return Client()

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
    region: str,
    flake_dir: str = "",
) -> None:
    """Provision one pool member; the registration token is already stored.

    The token reaches the VM as a root-only file via the secret_files
    attach, present at first boot - no post-boot seeding step exists. An
    unbuilt (rev, attr) template compiles server-side on first boot
    (single-flight per pinned rev; idempotency_key is refused for flake-ref
    templates, so none is sent).
    """
    # A pool defined in a subflake addresses it with `?dir=`; the rev pin
    # still names the whole repo, so the cache key stays (rev, attr).
    dir_part = f"?dir={flake_dir}" if flake_dir else ""
    options = create_options(
        template=f"github:{repo}/{rev}{dir_part}#{prefix}-{member}",
        name=name,
        region=region,
        secret_files={secret_name: "runner-token"},
    )
    await asyncio.wait_for(ix.machines().create(options), CREATE_TIMEOUT)
