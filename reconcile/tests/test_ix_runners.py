from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import datetime
import email.message
import enum
import http.server
import io
import json
import os
import pathlib
import re
import sys
import tempfile
import textwrap
import subprocess
import threading
import time
import unittest
import urllib.error
import urllib.request
from unittest import mock

from reconcile.config import DEFAULT_SPEC_PATH, SPEC_KEYS, Config, desired_rev
from reconcile.github import (
    MAX_DEMAND_RUNS,
    OPENER,
    api_url,
    deregister_member,
    extra_members,
    github_api,
    list_runners,
    member_online,
    member_runners,
    parse_time,
    pool_can_serve,
    pool_slots,
    run_ids,
    runner_label_sets,
)
from reconcile import vending
from reconcile.ix_runners import main, reconcile, require_hosted_runner
from reconcile.machines import MAX_PROBE_OUTPUT, probe_member
from reconcile.model import machine_status

REV = "a" * 40
OLD_REV = "b" * 40
MARKER = "/var/lib/ix-runner/repaired"
DAY_MS = 24 * 60 * 60 * 1000

ENV = {
    "IX_TOKEN": "ix_test_token",
    "RUNNER_PAT": "github_pat_test",
    "GITHUB_REPOSITORY": "example/baml",
    "POOL_SIZE": "2",
    "MAX_REPLACEMENTS": "2",
    "REGION": "us-east-1",
    "GITHUB_RUN_NUMBER": "0",
}


def now_ms() -> int:
    return int(time.time() * 1000)


def http_error(code: int, message: str) -> urllib.error.HTTPError:
    """An HTTPError carrying a real body, as GitHub's do: the 422 classifier
    reads the body, so a body-less fake would wave a misclassification
    through."""
    body = io.BytesIO(json.dumps({"message": message}).encode())
    return urllib.error.HTTPError(
        "https://api.github.com", code, message, email.message.Message(), body
    )


class FakeIxError(RuntimeError):
    """Mirrors ix_sdk.IxError, which subclasses RuntimeError."""


class FakeNotFound(FakeIxError):
    pass


class FakeUnavailable(FakeIxError):
    """What the SDK raises when the guest agent cannot be reached."""


class FakeStatus(enum.StrEnum):
    """Mirrors MachineStatus, which is a StrEnum with LOWERCASE values and
    exactly three states - there is no "starting": a machine coming up
    already reports RUNNING, which is why warming is read off started_at."""

    RUNNING = "running"
    STOPPED = "stopped"
    FAILED = "failed"


class FakeInfo:
    """Mirrors MachineInfo: every timestamp is Unix epoch MILLISECONDS, and
    started_at/stopped_at are None on a machine that never has."""

    def __init__(
        self,
        name,
        *,
        created_at=None,
        status=FakeStatus.RUNNING,
        failure_reason=None,
        started_at=None,
        stopped_at=None,
    ):
        self.name = name
        self.id = f"id-{name}"
        self.created_at = now_ms() - DAY_MS if created_at is None else created_at
        self.status = status
        self.failure_reason = failure_reason
        self.started_at = started_at
        self.stopped_at = stopped_at


class FakeExec:
    """Mirrors the SDK's ExecResult: exit_code is a PROPERTY, not a method
    (calling it was a live failure the method-shaped fake waved through)."""

    def __init__(self, code, stdout=""):
        self._code = code
        self.stdout = stdout
        self.stderr = ""

    @property
    def exit_code(self):
        return self._code


class FakeMachine:
    """One VM: scripted rev/marker state; records guest commands."""

    def __init__(self, platform, name):
        self.platform = platform
        self.name = name

    async def shell(self, script, working_dir=None):
        self.platform.calls.append((self.name, ("shell", script)))
        error = self.platform.probe_errors.get(self.name)
        if error is not None:
            raise error
        if self.name not in self.platform.revs:
            raise FakeUnavailable("guest agent did not answer")
        out = self.platform.revs[self.name] + "\n"
        if self.name in self.platform.markers:
            out += "ix-runner-strike\n"
        if f"rm -f {MARKER}" in script:
            self.platform.markers.discard(self.name)
        return FakeExec(0, out)

    async def exec(self, command, working_dir=None):
        self.platform.calls.append((self.name, tuple(command)))
        return FakeExec(0)

    async def delete(self):
        self.platform.calls.append((self.name, ("delete",)))

    async def start(self):
        # start() waits for the platform to report Running, then returns the
        # machine's row - it does NOT wait for a runner to register.
        self.platform.calls.append((self.name, ("start",)))
        self.platform.started.append(self.name)
        return FakeInfo(self.name, status=FakeStatus.RUNNING, started_at=now_ms())

    async def stop(self):
        self.platform.calls.append((self.name, ("stop",)))
        self.platform.stopped_by_run.append(self.name)
        return FakeInfo(self.name, status=FakeStatus.STOPPED, stopped_at=now_ms())


class FakeMachines:
    def __init__(self, platform):
        self.platform = platform

    async def list(self):
        return [FakeInfo(name, **self.platform.info.get(name, {})) for name in sorted(self.platform.vms)]

    def connect(self, vm_id):
        name = vm_id.removeprefix("id-")
        # One-shot: the scripted failure belongs to the probe that provoked
        # it, so the later execute-phase connect for the same member works.
        error = self.platform.connect_errors.pop(name, None)
        if error is not None:
            raise error
        return FakeMachine(self.platform, name)

    async def create(self, options):
        name = options["name"]
        self.platform.calls.append((None, ("create", options)))
        error = self.platform.create_errors.get(name, self.platform.create_error)
        # A dict scripts PER-REGION failure: {"us-east-1": err} fails creates
        # routed there and lets the failover attempt in another region pass.
        if isinstance(error, dict):
            error = error.get(options["region"])
        if error is not None:
            raise error
        self.platform.in_flight += 1
        self.platform.max_in_flight = max(
            self.platform.max_in_flight, self.platform.in_flight
        )
        await asyncio.sleep(self.platform.create_delay)
        self.platform.in_flight -= 1
        self.platform.created.append(name)


class FakeSecrets:
    def __init__(self, platform):
        self.platform = platform

    async def set(self, name, value):
        self.platform.calls.append((None, ("secret-set", name, value)))

    async def delete(self, name):
        self.platform.calls.append((None, ("secret-delete", name)))


class FakeIx:
    """Scripted ix SDK client; records every effectful call."""

    def __init__(
        self,
        *,
        vms,
        revs,
        online,
        markers,
        busy=frozenset(),
        busy_at_delete=frozenset(),
        registered=None,
        slots=1,
        info=None,
        broken=False,
        create_error=None,
        create_errors=None,
        connect_errors=None,
        probe_errors=None,
        page_size=100,
        jobs=None,
        labels=("self-hosted", "ix", "ix-linux-x64", "X64", "Linux"),
        demand_error=None,
        listing_error=None,
    ):
        self.vms = vms  # existing VM names
        self.revs = revs  # name -> baked config rev (missing = unreachable)
        self.online = set(online)  # pool member numbers with an online runner
        self.markers = set(markers)  # names carrying the two-strike marker
        self.busy = set(busy)  # pool member numbers with a runner mid-job
        # members that pick up a job AFTER the scan snapshot: idle in the
        # runners listing, but GitHub 422s the registration delete.
        self.busy_at_delete = set(busy_at_delete)
        # every member with runner registrations; online iff also in `online`.
        self.registered = set(self.online | self.busy if registered is None else registered)
        self.slots = slots  # runner daemons per VM
        self.info = info or {}  # name -> FakeInfo kwargs
        self.create_error = (
            create_error
            if create_error is not None
            else (FakeIxError("template build failed") if broken else None)
        )
        self.create_errors = create_errors or {}  # name -> exception
        self.connect_errors = connect_errors or {}  # name -> exception
        self.probe_errors = probe_errors or {}  # name -> exception from shell()
        self.page_size = page_size
        # One job per run, in the shape the scan reads: status, labels, and
        # for completed ones runner_name + completed_at.
        self.jobs = list(jobs or [])
        # What every runner in this pool advertises.
        self.labels = list(labels)
        self.runner_listings = 0
        self.demand_tokens = []  # which credential read the job queue
        self.demand_error = demand_error
        # Raised from the SECOND runner listing on - the stop path's fresh
        # re-read, never the initial scan.
        self.listing_error = listing_error
        self.calls = []
        self.started = []  # names started by this run
        self.stopped_by_run = []  # names stopped by this run
        self.created = []  # names whose create ran to completion
        self.create_delay = 0.0
        self.in_flight = 0
        self.max_in_flight = 0
        self.deletes_in_flight = 0
        self.max_deletes_in_flight = 0
        self.creates_during_a_delete = 0
        self.delete_delay = 0.0

    def machines(self):
        return FakeMachines(self)

    def secrets(self):
        return FakeSecrets(self)

    def runner_rows(self, busy):
        return [
            {
                "id": 1000 + member * 10 + slot,
                "name": f"baml-r{member}-{slot}",
                "status": "online" if member in self.online else "offline",
                "busy": member in busy,
                # GitHub returns labels as objects, and the implicit
                # self-hosted/arch/os ones sit alongside the configured set.
                "labels": [{"name": name} for name in self.labels],
            }
            for member in sorted(self.registered)
            for slot in range(1, self.slots + 1)
        ]

    def github_api(self, token, repo, path, *, method="GET", pat=True):
        self.calls.append((None, (method, path)))
        if path.startswith("/actions/runners?"):
            page = int(path.rsplit("page=", 1)[1])
            if page == 1:
                self.runner_listings += 1
            if self.listing_error is not None and self.runner_listings > 1:
                raise self.listing_error
            rows = self.runner_rows(self.busy)
            start = (page - 1) * self.page_size
            return {
                "total_count": len(rows),
                "runners": rows[start : start + self.page_size],
            }
        if path.startswith("/actions/runs?"):
            self.demand_tokens.append(token)
            if self.demand_error is not None:
                raise self.demand_error
            status = path.split("status=", 1)[1].split("&", 1)[0]
            # One run per job, which is the worst case for the scan.
            runs = [
                {"id": 500 + i}
                for i, job in enumerate(self.jobs)
                if job["status"] == status
            ]
            return {"total_count": len(runs), "workflow_runs": runs}
        if path.startswith("/actions/runs/") and "/jobs" in path:
            run_id = int(path.split("/actions/runs/", 1)[1].split("/", 1)[0])
            return {"total_count": 1, "jobs": [self.jobs[run_id - 500]]}
        if path == "/actions/runners/registration-token":
            return {"token": "REGTOKEN"}
        if method == "DELETE" and path.startswith("/actions/runners/"):
            self.deletes_in_flight += 1
            self.max_deletes_in_flight = max(
                self.max_deletes_in_flight, self.deletes_in_flight
            )
            try:
                time.sleep(self.delete_delay)
                self.creates_during_a_delete = max(
                    self.creates_during_a_delete, self.in_flight
                )
                member = (int(path.rsplit("/", 1)[1]) - 1000) // 10
                if member in self.busy_at_delete:
                    raise http_error(422, f"baml-r{member} is still running a job")
                return {}
            finally:
                self.deletes_in_flight -= 1
        raise AssertionError(f"unexpected API path {path}")


class ReconcileTest(unittest.IsolatedAsyncioTestCase):
    async def reconcile_with(self, ix, env=ENV):
        with (
            mock.patch.dict("os.environ", env, clear=True),
            mock.patch("reconcile.ix_runners.desired_rev", return_value=REV),
            mock.patch("reconcile.github.github_api", ix.github_api),
            mock.patch("reconcile.machines.create_options", lambda **kw: kw),
            mock.patch("reconcile.github.DEREGISTER_PAUSE", 0.0),
        ):
            return await reconcile(ix)

    async def test_missing_member_is_created_with_secret_at_boot(self):
        ix = FakeIx(vms=set(), revs={}, online=set(), markers=set())
        self.assertEqual(await self.reconcile_with(ix), 2)
        # ONE token mint and ONE secret store per run, however many members
        # are created: registration tokens are repo-scoped and hour-valid.
        secret_sets = [c for _, c in ix.calls if c[0] == "secret-set"]
        self.assertEqual(
            secret_sets, [("secret-set", "baml_runner_reg_token", "REGTOKEN")]
        )
        mints = [
            c
            for _, c in ix.calls
            if c == ("POST", "/actions/runners/registration-token")
        ]
        self.assertEqual(len(mints), 1)
        creates = [c for _, c in ix.calls if c[0] == "create"]
        self.assertEqual(
            creates[0][1],
            {
                "template": f"github:example/baml/{REV}#ci-runner-1",
                "name": "baml-runner-1",
                "region": "us-east-1",
                "secret_files": {"baml_runner_reg_token": "runner-token"},
            },
        )

    async def test_flake_dir_pins_the_template_to_the_subflake(self):
        # Same create, pool defined in a subflake: the ref gains ?dir= and
        # nothing else moves (the rev still names the whole repo, so the
        # server's (rev, attr) template cache key is unchanged in shape).
        ix = FakeIx(vms=set(), revs={}, online=set(), markers=set())
        self.assertEqual(
            await self.reconcile_with(ix, ENV | {"FLAKE_DIR": "nix/ix"}), 2
        )
        creates = [c for _, c in ix.calls if c[0] == "create"]
        self.assertEqual(
            creates[0][1]["template"],
            f"github:example/baml/{REV}?dir=nix/ix#ci-runner-1",
        )

    async def test_a_failed_create_retries_once_in_the_next_region(self):
        # Two-region pool; member 1's home region (index 1 % 2 -> the second
        # entry) is scripted sick. The create must retry once in the OTHER
        # region this tick - replacements flowing back into a dying region
        # is how a regional incident ate the whole pool once - and the
        # healthy member 2 must land in its own home untouched.
        env = dict(ENV)
        env.pop("REGION")
        env["REGIONS"] = "us-west-1,us-east-1"
        ix = FakeIx(
            vms=set(), revs={}, online=set(), markers=set(),
            create_errors={
                "baml-runner-1": {"us-east-1": RuntimeError("hosts sick")}
            },
        )
        await self.reconcile_with(ix, env=env)
        member1 = [
            c[1]["region"] for _, c in ix.calls
            if c[0] == "create" and c[1]["name"] == "baml-runner-1"
        ]
        self.assertEqual(
            member1, ["us-east-1", "us-west-1"],
            "home first, then exactly one failover attempt",
        )
        self.assertIn("baml-runner-1", ix.created)
        member2 = [
            c[1]["region"] for _, c in ix.calls
            if c[0] == "create" and c[1]["name"] == "baml-runner-2"
        ]
        self.assertEqual(member2, ["us-west-1"])

    async def test_the_registration_token_is_masked_and_the_row_is_kept(self):
        # The token can register a job-stealing runner for an hour, so it
        # must never land unmasked in a log. The secret ROW, though, is now
        # deliberately kept: deleting it would make the next write an insert
        # rather than an overwrite, and only an overwrite propagates a
        # rotation to machines that already hold a copy - which is the only
        # way a stopped member ever receives a usable token again.
        ix = FakeIx(vms=set(), revs={}, online=set(), markers=set())
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            await self.reconcile_with(ix)
        self.assertIn("::add-mask::REGTOKEN", out.getvalue())
        self.assertEqual([c for _, c in ix.calls if c[0] == "secret-delete"], [])

    async def test_the_mask_is_flushed_before_the_token_is_used(self):
        # Python block-buffers stdout to a pipe, so an unflushed mask can
        # still be sitting in this process while the very next call writes the
        # token into a traceback. The mask has to be out first, not merely
        # printed first.
        ix = FakeIx(vms=set(), revs={}, online=set(), markers=set())
        printed = []

        def recording_print(*args, **kwargs):
            printed.append((args, kwargs))

        with mock.patch("builtins.print", recording_print):
            await self.reconcile_with(ix)
        masks = [
            kwargs
            for args, kwargs in printed
            if args and str(args[0]).startswith("::add-mask::")
        ]
        # Two secrets are masked - the RUNNER_PAT at the top of the run and the
        # registration token at mint - and every mask must be flushed.
        self.assertEqual(masks, [{"flush": True}, {"flush": True}])

    async def test_attr_prefix_is_configurable(self):
        ix = FakeIx(vms=set(), revs={}, online=set(), markers=set())
        env = dict(ENV, ATTR_PREFIX="runner", POOL_NAME="pool", POOL_SIZE="1")
        await self.reconcile_with(ix, env=env)
        creates = [c for _, c in ix.calls if c[0] == "create"]
        self.assertEqual(
            creates[0][1]["template"], f"github:example/baml/{REV}#runner-1"
        )
        self.assertEqual(creates[0][1]["name"], "pool-runner-1")

    async def test_stale_rev_is_replaced(self):
        ix = FakeIx(
            vms={"baml-runner-1", "baml-runner-2"},
            revs={"baml-runner-1": OLD_REV, "baml-runner-2": REV},
            online={2},
            markers=set(),
        )
        self.assertEqual(await self.reconcile_with(ix), 1)
        self.assertIn(("baml-runner-1", ("delete",)), ix.calls)
        self.assertNotIn(("baml-runner-2", ("delete",)), ix.calls)

    async def test_replace_deregisters_registrations_before_vm_delete(self):
        # The registration delete is the atomic guard (GitHub 422s a busy
        # runner); it must come before the VM is destroyed.
        ix = FakeIx(
            vms={"baml-runner-1", "baml-runner-2"},
            revs={"baml-runner-1": OLD_REV, "baml-runner-2": REV},
            online={1, 2},
            markers=set(),
        )
        self.assertEqual(await self.reconcile_with(ix), 1)
        dereg = ix.calls.index((None, ("DELETE", "/actions/runners/1011")))
        vm_delete = ix.calls.index(("baml-runner-1", ("delete",)))
        self.assertLess(dereg, vm_delete)

    async def test_mid_scan_job_pickup_defers_the_replace(self):
        # Member 1 reads idle in the scan snapshot but picks up a job before
        # the delete: GitHub 422s the deregister, the member is skipped with
        # no budget spent, and the other stale member still converges.
        ix = FakeIx(
            vms={"baml-runner-1", "baml-runner-2"},
            revs={"baml-runner-1": OLD_REV, "baml-runner-2": OLD_REV},
            online={1, 2},
            markers=set(),
            busy_at_delete={1},
        )
        self.assertEqual(await self.reconcile_with(ix), 1)
        self.assertNotIn(("baml-runner-1", ("delete",)), ix.calls)
        self.assertIn(("baml-runner-2", ("delete",)), ix.calls)

    async def test_deregistrations_never_overlap(self):
        # Concurrent DELETEs trip GitHub's secondary rate limit, whose 422 is
        # indistinguishable by status from a busy runner: serialize them.
        ix = FakeIx(
            vms={f"baml-runner-{m}" for m in range(1, 5)},
            revs={f"baml-runner-{m}": OLD_REV for m in range(1, 5)},
            online={1, 2, 3, 4},
            markers=set(),
            slots=2,
        )
        ix.delete_delay = 0.01
        env = dict(ENV, POOL_SIZE="4", MAX_REPLACEMENTS="4", CONCURRENCY="4")
        self.assertEqual(await self.reconcile_with(ix, env=env), 4)
        self.assertEqual(ix.max_deletes_in_flight, 1)

    async def test_a_deregistration_does_not_stall_a_sibling_create(self):
        # deregister_member is blocking urllib: called on the event loop it
        # freezes every sibling action, eating their CREATE_TIMEOUT budget.
        ix = FakeIx(
            vms={"baml-runner-1"},
            revs={"baml-runner-1": OLD_REV},
            online={1},
            markers=set(),
        )
        ix.delete_delay = 0.05
        ix.create_delay = 0.05
        env = dict(ENV, POOL_SIZE="2", MAX_REPLACEMENTS="2", CONCURRENCY="2")
        await self.reconcile_with(ix, env=env)
        self.assertGreater(ix.creates_during_a_delete, 0)

    async def test_unreachable_member_is_replaced(self):
        ix = FakeIx(
            vms={"baml-runner-1", "baml-runner-2"},
            revs={"baml-runner-2": REV},
            online={2},
            markers=set(),
        )
        self.assertEqual(await self.reconcile_with(ix), 1)
        self.assertIn(("baml-runner-1", ("delete",)), ix.calls)

    async def test_young_unreachable_member_is_left_alone(self):
        # A first boot compiles the template in-guest: silence from a machine
        # created minutes ago is the build running, not a dead VM.
        ix = FakeIx(
            vms={"baml-runner-1", "baml-runner-2"},
            revs={"baml-runner-2": REV},
            online={2},
            markers=set(),
            info={"baml-runner-1": {"created_at": now_ms() - 60_000}},
        )
        self.assertEqual(await self.reconcile_with(ix), 0)
        self.assertNotIn(("baml-runner-1", ("delete",)), ix.calls)

    async def test_offline_member_is_repaired_first(self):
        ix = FakeIx(
            vms={"baml-runner-1", "baml-runner-2"},
            revs={"baml-runner-1": REV, "baml-runner-2": REV},
            online={2},
            markers=set(),
        )
        self.assertEqual(await self.reconcile_with(ix), 0)
        self.assertIn(
            ("baml-runner-1", ("systemctl", "restart", "github-runner-*")),
            ix.calls,
        )
        self.assertIn(("baml-runner-1", ("touch", MARKER)), ix.calls)
        self.assertNotIn(("baml-runner-1", ("delete",)), ix.calls)

    async def test_offline_member_with_strike_is_replaced(self):
        ix = FakeIx(
            vms={"baml-runner-1", "baml-runner-2"},
            revs={"baml-runner-1": REV, "baml-runner-2": REV},
            online={2},
            markers={"baml-runner-1"},
        )
        self.assertEqual(await self.reconcile_with(ix), 1)
        self.assertIn(("baml-runner-1", ("delete",)), ix.calls)

    async def test_healthy_member_clears_the_marker_in_the_probe(self):
        # One guest round-trip per healthy member: the rev read and the marker
        # clear share a shell.
        ix = FakeIx(
            vms={"baml-runner-1", "baml-runner-2"},
            revs={"baml-runner-1": REV, "baml-runner-2": REV},
            online={1, 2},
            markers={"baml-runner-1"},
        )
        self.assertEqual(await self.reconcile_with(ix), 0)
        shells = [c[1] for name, c in ix.calls if name == "baml-runner-1"]
        self.assertEqual(len(shells), 1)
        self.assertIn(f"rm -f {MARKER}", shells[0])
        self.assertNotIn("baml-runner-1", ix.markers)

    async def test_offline_member_probe_does_not_clear_the_marker(self):
        # Clearing the strike on an offline member would make the two-strike
        # replace unreachable: it would repair forever.
        ix = FakeIx(
            vms={"baml-runner-1"},
            revs={"baml-runner-1": REV},
            online=set(),
            markers={"baml-runner-1"},
        )
        env = dict(ENV, POOL_SIZE="1", MAX_REPLACEMENTS="0")
        await self.reconcile_with(ix, env=env)
        shells = [c[1] for name, c in ix.calls if name == "baml-runner-1" and c[0] == "shell"]
        self.assertNotIn(f"rm -f {MARKER}", shells[0])

    async def test_replacement_budget_caps_work_per_run(self):
        # A non-empty stale pool rolls at most MAX_REPLACEMENTS per run.
        vms = {f"baml-runner-{m}" for m in range(1, 9)}
        ix = FakeIx(
            vms=vms,
            revs={name: OLD_REV for name in vms},
            online=set(),
            markers=set(),
        )
        env = dict(ENV, POOL_SIZE="8", MAX_REPLACEMENTS="2")
        self.assertEqual(await self.reconcile_with(ix, env=env), 2)
        creates = [c for _, c in ix.calls if c[0] == "create"]
        self.assertEqual(len(creates), 2)

    async def test_budget_exhaustion_skips_rather_than_ends_the_pass(self):
        # Members past the budget still get their free work: with a `break`,
        # member 3's repair never ran and it stayed offline forever.
        ix = FakeIx(
            vms={"baml-runner-1", "baml-runner-2", "baml-runner-3"},
            revs={
                "baml-runner-1": OLD_REV,
                "baml-runner-2": OLD_REV,
                "baml-runner-3": REV,
            },
            online={1, 2},
            markers=set(),
        )
        env = dict(ENV, POOL_SIZE="3", MAX_REPLACEMENTS="1")
        self.assertEqual(await self.reconcile_with(ix, env=env), 1)
        self.assertIn(
            ("baml-runner-3", ("systemctl", "restart", "github-runner-*")),
            ix.calls,
        )

    async def test_scan_order_rotates_with_the_run_number(self):
        # A fixed order lets one unfixable low-numbered member own the whole
        # budget run after run; the rotation moves the starting point.
        def stale_pool():
            vms = {f"baml-runner-{m}" for m in range(1, 5)}
            return FakeIx(
                vms=vms,
                revs={name: OLD_REV for name in vms},
                online={1, 2, 3, 4},
                markers=set(),
            )

        base = dict(ENV, POOL_SIZE="4", MAX_REPLACEMENTS="1")
        first = stale_pool()
        await self.reconcile_with(first, env=dict(base, GITHUB_RUN_NUMBER="0"))
        self.assertIn(("baml-runner-1", ("delete",)), first.calls)

        second = stale_pool()
        await self.reconcile_with(second, env=dict(base, GITHUB_RUN_NUMBER="1"))
        self.assertNotIn(("baml-runner-1", ("delete",)), second.calls)
        self.assertIn(("baml-runner-2", ("delete",)), second.calls)

    async def test_members_above_pool_size_are_pruned(self):
        # Shrinking the pool must not orphan VMs: they keep billing and keep
        # taking jobs from a config nothing reconciles.
        ix = FakeIx(
            vms={"baml-runner-1", "baml-runner-2"},
            revs={"baml-runner-1": REV, "baml-runner-2": REV},
            online={1, 2},
            markers=set(),
        )
        env = dict(ENV, POOL_SIZE="1")
        await self.reconcile_with(ix, env=env)
        self.assertIn((None, ("DELETE", "/actions/runners/1021")), ix.calls)
        self.assertIn(("baml-runner-2", ("delete",)), ix.calls)
        self.assertEqual([c for _, c in ix.calls if c[0] == "create"], [])

    async def test_a_pool_size_of_zero_drains_the_pool(self):
        # Draining is the limit case of a shrink, not a crash.
        ix = FakeIx(
            vms={"baml-runner-1"},
            revs={"baml-runner-1": REV},
            online={1},
            markers=set(),
        )
        env = dict(ENV, POOL_SIZE="0", MAX_REPLACEMENTS="1")
        await self.reconcile_with(ix, env=env)
        self.assertIn(("baml-runner-1", ("delete",)), ix.calls)

    async def test_creates_run_concurrently_under_the_cap(self):
        # The execute phase overlaps guest boots but never exceeds the
        # semaphore; with 8 creates and concurrency 3, peak in-flight is 3.
        ix = FakeIx(vms=set(), revs={}, online=set(), markers=set())
        ix.create_delay = 0.01
        env = dict(ENV, POOL_SIZE="8", MAX_REPLACEMENTS="8", CONCURRENCY="3")
        self.assertEqual(await self.reconcile_with(ix, env=env), 8)
        self.assertGreater(ix.max_in_flight, 1)
        self.assertLessEqual(ix.max_in_flight, 3)

    async def test_probes_run_concurrently(self):
        # An all-unreachable pool used to pay EXEC_TIMEOUT per member, in
        # series, before anything happened.
        ix = FakeIx(
            vms={f"baml-runner-{m}" for m in range(1, 9)},
            revs={},
            online=set(),
            markers=set(),
        )
        env = dict(ENV, POOL_SIZE="8", MAX_REPLACEMENTS="0", CONCURRENCY="4")
        with mock.patch("reconcile.snapshot.probe_member") as probe:
            in_flight = 0
            peak = 0

            async def slow_probe(machine, *, clear_marker):
                nonlocal in_flight, peak
                in_flight += 1
                peak = max(peak, in_flight)
                await asyncio.sleep(0.01)
                in_flight -= 1
                return None, False

            probe.side_effect = slow_probe
            await self.reconcile_with(ix, env=env)
        self.assertEqual(peak, 4)

    async def test_empty_pool_bootstraps_past_the_cap(self):
        # First bootstrap: nothing exists to thrash, so the cap self-raises
        # and the whole pool builds in one run (no manual dispatch needed).
        ix = FakeIx(vms=set(), revs={}, online=set(), markers=set())
        env = dict(ENV, POOL_SIZE="8", MAX_REPLACEMENTS="2")
        self.assertEqual(await self.reconcile_with(ix, env=env), 8)

    async def test_stale_but_busy_member_is_deferred(self):
        # A config roll must never kill a running job: the busy member is
        # skipped without spending budget, and converges on a later run.
        ix = FakeIx(
            vms={"baml-runner-1", "baml-runner-2"},
            revs={"baml-runner-1": OLD_REV, "baml-runner-2": OLD_REV},
            online={1, 2},
            markers=set(),
            busy={1},
        )
        self.assertEqual(await self.reconcile_with(ix), 1)
        self.assertNotIn(("baml-runner-1", ("delete",)), ix.calls)
        self.assertIn(("baml-runner-2", ("delete",)), ix.calls)

    async def test_a_newline_in_a_remote_string_cannot_forge_a_summary_row(self):
        # Error text quotes remote strings (an ix failure_reason, a runner
        # name someone else chose). The summary escaped pipes but not
        # newlines, so one newline writes a row of the reader's choosing.
        ix = FakeIx(
            vms=set(),
            revs={},
            online=set(),
            markers=set(),
            create_error=FakeIxError("boom\n| baml-runner-9 | replace | ok |"),
        )
        with tempfile.TemporaryDirectory() as scratch:
            path = os.path.join(scratch, "summary.md")
            with (
                contextlib.redirect_stdout(io.StringIO()),
                self.assertRaises(SystemExit),
            ):
                await self.reconcile_with(ix, env=dict(ENV, GITHUB_STEP_SUMMARY=path))
            written = pathlib.Path(path).read_text()
        # A leading blank, the header, the separator, and exactly one line per
        # member. Any extra line is text that escaped its cell - pipe escaping
        # alone still lets a newline break the row in two.
        lines = written.splitlines()
        self.assertEqual(len(lines), 5, written)
        self.assertEqual(
            [line.split(" | ")[0] for line in lines[3:]],
            ["| baml-runner-1", "| baml-runner-2"],
        )

    async def test_a_newline_in_a_failure_reason_cannot_forge_a_log_line(self):
        # `::` workflow commands parse at the start of a line, so a newline
        # in remote text writes a command of its author's choosing.
        ix = FakeIx(
            vms={"baml-runner-1"},
            revs={},
            online=set(),
            markers=set(),
            info={"baml-runner-1": {"failure_reason": "dead\n::error::forged"}},
        )
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            await self.reconcile_with(
                ix, env=dict(ENV, POOL_SIZE="1", MAX_REPLACEMENTS="0")
            )
        self.assertNotIn("\n::error::forged", out.getvalue())
        # Still reported, just not on a line of its own.
        self.assertIn("forged", out.getvalue())

    async def test_one_hostile_member_does_not_cancel_the_other_probes(self):
        # A probe that raises anything but the three expected types used to
        # escape the gather, cancel every sibling probe, and kill the run -
        # freezing the pool, including a security rev bump, until a human
        # noticed. Every other member must still be decided.
        ix = FakeIx(
            vms={"baml-runner-1", "baml-runner-2"},
            revs={"baml-runner-1": REV, "baml-runner-2": OLD_REV},
            online={1, 2},
            markers=set(),
            connect_errors={"baml-runner-1": MemoryError("guest flooded the read")},
        )
        self.assertEqual(await self.reconcile_with(ix), 2)
        # Member 1 reads unreachable (the decision the loop already has for
        # a member that says nothing), member 2 still converges.
        self.assertIn(("baml-runner-2", ("delete",)), ix.calls)
        self.assertEqual(sorted(ix.created), ["baml-runner-1", "baml-runner-2"])

    async def test_a_member_that_defers_forever_is_called_out(self):
        # A member busy at every scan defers its own replacement for as long
        # as it likes, so a runner-config change never reaches it. Nothing
        # said so, and the deferral is silent by design.
        ix = FakeIx(
            vms={"baml-runner-1", "baml-runner-2"},
            revs={"baml-runner-1": OLD_REV, "baml-runner-2": OLD_REV},
            online={1, 2},
            busy={1, 2},
            markers=set(),
            info={"baml-runner-1": {"created_at": now_ms() - 60 * DAY_MS}},
        )
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(await self.reconcile_with(ix), 0)
        warnings = [
            line
            for line in out.getvalue().splitlines()
            if line.startswith("::warning::")
        ]
        # Exactly one: member 2 is busy and stale too, but a day old.
        self.assertEqual(len(warnings), 1)
        self.assertIn("baml-runner-1", warnings[0])
        # Warned about, never killed mid-job.
        self.assertNotIn(("baml-runner-1", ("delete",)), ix.calls)

    async def test_one_failed_create_does_not_abort_the_run(self):
        # A bad template rev spends the budget and exits non-zero, but every
        # member in budget is still attempted (no half-scanned pool).
        ix = FakeIx(vms=set(), revs={}, online=set(), markers=set(), broken=True)
        with self.assertRaises(SystemExit):
            await self.reconcile_with(ix)
        creates = [c for _, c in ix.calls if c[0] == "create"]
        self.assertEqual(len(creates), 2)

    async def test_an_unforeseen_exception_is_reported_as_that_member_failing(self):
        # Fault isolation caught only RuntimeError-rooted errors, so anything
        # else escaped the handler and lost the member/action attribution.
        ix = FakeIx(
            vms=set(),
            revs={},
            online=set(),
            markers=set(),
            create_error=ValueError("not an SDK error"),
        )
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit):
            await self.reconcile_with(ix)
        self.assertEqual(len([c for _, c in ix.calls if c[0] == "create"]), 2)
        self.assertIn("::error::baml-runner-1: create FAILED", out.getvalue())

    async def test_a_straggler_exception_does_not_cancel_a_sibling_mid_create(self):
        # An exception the handler cannot catch (BaseException, a cancelled
        # child) used to abort the gather and kill in-flight creates halfway.
        ix = FakeIx(
            vms=set(),
            revs={},
            online=set(),
            markers=set(),
            create_errors={"baml-runner-1": asyncio.CancelledError()},
        )
        ix.create_delay = 0.05
        env = dict(ENV, CONCURRENCY="2")
        with self.assertRaises(SystemExit):
            await self.reconcile_with(ix, env=env)
        self.assertEqual(ix.created, ["baml-runner-2"])


AUTO_ENV = ENV | {
    "POOL_SIZE": "6",
    "MIN_WARM": "1",
    "SCALE_HEADROOM": "0",
    "RUNNER_LABEL": "ix",
    "GITHUB_TOKEN": "ghs_workflow_token",
    "GITHUB_EVENT_NAME": "schedule",
}

# Labels a pool runner advertises, and two label sets it cannot serve.
POOL_LABELS = ["self-hosted", "ix", "ix-linux-x64"]
FOREIGN = ["blacksmith-4vcpu-ubuntu-2404"]


def job(status="queued", labels=None):
    """One active job, in the shape the demand scan reads: `labels` is the
    job's `runs-on` set, which GitHub ANDs."""
    return {"status": status, "labels": list(labels or POOL_LABELS)}


def finished(runner="baml-r2-1", ago=3600):
    """One completed job, which is what the idle clock is derived from."""
    return {
        "status": "completed",
        "labels": list(POOL_LABELS),
        "runner_name": runner,
        "completed_at": iso(ago),
    }


def iso(ago):
    return (
        datetime.datetime.fromtimestamp(time.time() - ago, datetime.UTC)
        .isoformat()
        .replace("+00:00", "Z")
    )


def autoscale_pool(*, running=(), stopped=(), failed=(), online=(), **kwargs):
    """A pool whose members are in the given power states.

    Only running members get a rev: a stopped one that gets probed anyway
    raises out of the fake guest, reads as unreachable, and is replaced -
    precisely the failure these tests exist to catch, so it must not be
    papered over here.

    Every member that exists is registered on GitHub. Stopped ones included:
    these tests cover both the pre-deregister world and the current one, and
    a test that needs a member with NO registrations passes `registered`.
    """

    def names(members):
        return {f"baml-runner-{m}" for m in members}

    info = {
        f"baml-runner-{m}": {"status": FakeStatus.STOPPED, "stopped_at": now_ms()}
        for m in stopped
    }
    info |= {
        f"baml-runner-{m}": {
            "status": FakeStatus.FAILED,
            "failure_reason": "the host lost the guest",
        }
        for m in failed
    }
    # Merged per member, not per dict: an override that adds a timestamp must
    # not silently drop the status this helper just set for it.
    for name, extra in kwargs.pop("info", {}).items():
        info[name] = info.get(name, {}) | extra
    kwargs.setdefault("registered", set(running) | set(stopped) | set(failed))
    return FakeIx(
        vms=names(running) | names(stopped) | names(failed),
        revs={f"baml-runner-{m}": REV for m in running},
        online=set(online),
        markers=set(),
        info=info,
        **kwargs,
    )


async def run_reconcile(ix, env):
    with (
        mock.patch.dict("os.environ", env, clear=True),
        mock.patch("reconcile.ix_runners.desired_rev", return_value=REV),
        mock.patch("reconcile.github.github_api", ix.github_api),
        mock.patch("reconcile.machines.create_options", lambda **kw: kw),
        mock.patch("reconcile.github.DEREGISTER_PAUSE", 0.0),
        contextlib.redirect_stdout(io.StringIO()),
    ):
        return await reconcile(ix)


class AutoscaleTest(unittest.IsolatedAsyncioTestCase):
    """Power state is the only dynamic axis: the member set is declarative,
    and every test here is about which of those members are switched on."""

    async def test_an_unconfigured_pool_never_scales_and_never_asks_demand(self):
        # MIN_WARM defaults to POOL_SIZE, so desired is pinned to the whole
        # pool and no clamp can move it. Asking GitHub for a number that
        # cannot change the answer is a wasted scan on every tick.
        ix = autoscale_pool(running=[1, 2], online=[1, 2])
        self.assertEqual(await run_reconcile(ix, ENV | {"POOL_SIZE": "2"}), 0)
        self.assertEqual(ix.started, [])
        self.assertEqual(ix.stopped_by_run, [])
        self.assertEqual(
            [c for _, c in ix.calls if c[0] == "GET" and "/actions/runs" in c[1]], []
        )

    async def test_a_stopped_member_is_never_probed_and_never_replaced(self):
        # The whole feature stands on this. The decide loop reads an
        # unanswered probe as "unreachable -> replace", so probing a machine
        # that is switched off would delete and rebuild every member
        # autoscaling had just parked - at a template build each.
        ix = autoscale_pool(running=[1], online=[1], stopped=[2, 3, 4, 5, 6])
        self.assertEqual(await run_reconcile(ix, AUTO_ENV), 0)
        self.assertEqual({n for n, c in ix.calls if c[0] == "shell"}, {"baml-runner-1"})
        self.assertEqual([c for _, c in ix.calls if c[0] == "create"], [])
        self.assertEqual([n for n, c in ix.calls if c == ("delete",)], [])
        self.assertEqual(ix.started, [])
        self.assertEqual(ix.stopped_by_run, [])

    async def test_a_surplus_stops_the_highest_indexed_idle_members(self):
        # Highest index first, so the warm core is always the same low
        # members and their template and toolchain caches stay hot.
        ix = autoscale_pool(
            running=[1, 2, 3, 4], online=[1, 2, 3, 4], jobs=[finished()]
        )
        await run_reconcile(ix, AUTO_ENV)
        self.assertEqual(
            ix.stopped_by_run, ["baml-runner-4", "baml-runner-3", "baml-runner-2"]
        )

    async def test_min_warm_is_a_floor_under_the_scale_down(self):
        ix = autoscale_pool(running=[1, 2, 3], online=[1, 2, 3], jobs=[finished()])
        await run_reconcile(ix, AUTO_ENV | {"MIN_WARM": "2"})
        self.assertEqual(ix.stopped_by_run, ["baml-runner-3"])

    async def test_min_warm_holds_when_demand_is_zero(self):
        ix = autoscale_pool(
            running=[1, 2], online=[1, 2], stopped=[3, 4, 5, 6], jobs=[finished()]
        )
        await run_reconcile(ix, AUTO_ENV | {"MIN_WARM": "4"})
        self.assertEqual(ix.started, ["baml-runner-3", "baml-runner-4"])
        self.assertEqual(ix.stopped_by_run, [])

    async def test_scale_up_starts_a_stopped_member_rather_than_creating(self):
        # A start is seconds; a create is a template build.
        ix = autoscale_pool(
            running=[1], online=[1], stopped=[2, 3, 4, 5, 6], jobs=[job()] * 3
        )
        await run_reconcile(ix, AUTO_ENV)
        self.assertEqual(ix.started, ["baml-runner-2", "baml-runner-3"])
        self.assertEqual(ix.created, [])

    async def test_headroom_is_added_on_top_of_demand(self):
        ix = autoscale_pool(
            running=[1], online=[1], stopped=[2, 3, 4, 5, 6], jobs=[job()]
        )
        await run_reconcile(ix, AUTO_ENV | {"SCALE_HEADROOM": "2"})
        self.assertEqual(ix.started, ["baml-runner-2", "baml-runner-3"])

    async def test_max_online_caps_the_scale_up(self):
        ix = autoscale_pool(
            running=[1], online=[1], stopped=[2, 3, 4, 5, 6], jobs=[job()] * 5
        )
        await run_reconcile(ix, AUTO_ENV | {"MAX_ONLINE": "2"})
        self.assertEqual(ix.started, ["baml-runner-2"])

    async def test_a_leftover_job_still_needs_a_whole_machine(self):
        # 5 jobs across 4 slots is two machines, not one: the division has to
        # round UP. 8/4 would pass either way, which is why it is not 8.
        ix = autoscale_pool(
            running=[1], online=[1], stopped=[2, 3, 4, 5, 6], slots=4, jobs=[job()] * 5
        )
        await run_reconcile(ix, AUTO_ENV)
        self.assertEqual(ix.started, ["baml-runner-2"])

    async def test_demand_counts_queued_and_in_progress_jobs(self):
        # A wave is usually a fan-out inside a run that is already
        # in_progress; counting only queued work misses exactly the demand
        # the pool exists to absorb.
        ix = autoscale_pool(
            running=[1],
            online=[1],
            stopped=[2, 3, 4, 5, 6],
            jobs=[job(), job("in_progress")],
        )
        await run_reconcile(ix, AUTO_ENV)
        self.assertEqual(ix.started, ["baml-runner-2"])


class LabelSatisfactionTest(unittest.IsolatedAsyncioTestCase):
    """Demand counts jobs this pool could actually take, and nothing else."""

    async def test_a_queued_job_with_a_foreign_label_is_not_demand(self):
        # THE case that makes strict matching non-negotiable. This repo has
        # runs queued indefinitely against blacksmith-* labels no ix runner
        # carries - jobs nothing here will ever serve. Counting them is not
        # conservative, it pins the pool at max-online forever on work it
        # cannot do.
        ix = autoscale_pool(
            running=[1],
            online=[1],
            stopped=[2, 3, 4, 5, 6],
            jobs=[job(labels=FOREIGN) for _ in range(5)] + [finished()],
        )
        await run_reconcile(ix, AUTO_ENV)
        self.assertEqual(ix.started, [])
        # And with zero real demand it is a scale-DOWN, not a hold at max.
        self.assertEqual(ix.stopped_by_run, [])  # already at min_warm 1

    async def test_labels_are_anded_so_a_partial_match_is_not_demand(self):
        # GitHub requires a runner to carry EVERY label in runs-on. A job
        # asking for [self-hosted, ix, gpu] cannot run here even though two
        # of its three labels match.
        ix = autoscale_pool(
            running=[1],
            online=[1],
            stopped=[2, 3, 4, 5, 6],
            jobs=[job(labels=["self-hosted", "ix", "gpu"])] * 4,
        )
        await run_reconcile(ix, AUTO_ENV)
        self.assertEqual(ix.started, [])

    async def test_a_subset_of_the_advertised_labels_is_demand(self):
        # The converse: a job needing fewer labels than a runner advertises
        # runs fine, so it counts.
        ix = autoscale_pool(
            running=[1],
            online=[1],
            stopped=[2, 3, 4, 5, 6],
            jobs=[job(labels=["self-hosted", "ix"])] * 2,
        )
        await run_reconcile(ix, AUTO_ENV)
        self.assertEqual(ix.started, ["baml-runner-2"])

    async def test_foreign_jobs_do_not_hold_the_pool_up(self):
        # The full shape of the bug: a big foreign queue alongside a small
        # real one sizes the pool to the real one.
        ix = autoscale_pool(
            running=[1],
            online=[1],
            stopped=[2, 3, 4, 5, 6],
            jobs=[job(labels=FOREIGN) for _ in range(20)] + [job(), job()],
        )
        await run_reconcile(ix, AUTO_ENV)
        self.assertEqual(ix.started, ["baml-runner-2"])


class TickModeTest(unittest.IsolatedAsyncioTestCase):
    async def test_an_event_tick_never_stops_a_member(self):
        # A workflow_run tick fires when a run is REQUESTED - before its jobs
        # reach the queue. The pool looks idle precisely because the wave has
        # not landed, so a scale-down here switches machines off at the start
        # of a wave.
        ix = autoscale_pool(
            running=[1, 2, 3, 4], online=[1, 2, 3, 4], jobs=[finished()]
        )
        await run_reconcile(ix, AUTO_ENV | {"GITHUB_EVENT_NAME": "workflow_run"})
        self.assertEqual(ix.stopped_by_run, [])

    async def test_an_event_tick_still_starts_members(self):
        ix = autoscale_pool(
            running=[1], online=[1], stopped=[2, 3, 4, 5, 6], jobs=[job()] * 2
        )
        await run_reconcile(ix, AUTO_ENV | {"GITHUB_EVENT_NAME": "workflow_run"})
        self.assertEqual(ix.started, ["baml-runner-2"])

    async def test_a_scheduled_tick_may_stop(self):
        # The control for the two above: same pool, same surplus, cron tick.
        ix = autoscale_pool(
            running=[1, 2, 3, 4], online=[1, 2, 3, 4], jobs=[finished()]
        )
        await run_reconcile(ix, AUTO_ENV | {"GITHUB_EVENT_NAME": "schedule"})
        self.assertEqual(len(ix.stopped_by_run), 3)


class ScaleDownMechanicsTest(unittest.IsolatedAsyncioTestCase):
    async def test_a_stop_deregisters_before_cutting_the_power(self):
        # The order IS the lock: after the DELETE the runner cannot be
        # assigned anything, which is a guarantee no amount of re-reading a
        # listing can give.
        ix = autoscale_pool(
            running=[1, 2], online=[1, 2], jobs=[finished(runner="baml-r2-1")]
        )
        await run_reconcile(ix, AUTO_ENV)
        self.assertEqual(ix.stopped_by_run, ["baml-runner-2"])
        deletes = [
            i
            for i, (_, c) in enumerate(ix.calls)
            if c[0] == "DELETE" and c[1].startswith("/actions/runners/")
        ]
        stop = next(
            i for i, (n, c) in enumerate(ix.calls) if c == ("stop",)
        )
        self.assertTrue(deletes and max(deletes) < stop)

    async def test_a_member_that_takes_a_job_first_refuses_its_own_stop(self):
        # GitHub answers the registration DELETE with 422 for a busy runner.
        # That refusal is the whole safety story for scale-down.
        ix = autoscale_pool(
            running=[1, 2],
            online=[1, 2],
            busy_at_delete={2},
            jobs=[finished(runner="baml-r2-1")],
        )
        await run_reconcile(ix, AUTO_ENV)
        self.assertEqual(ix.stopped_by_run, [])

    async def test_a_busy_member_is_passed_over_for_the_next_idle_one(self):
        # Two layers protect a running job; this pins the first. Asserting
        # only that the busy one stayed up proves nothing, because the 422
        # would catch it anyway - and then the scale-down slot is spent on a
        # member that was never stoppable, so the pool never shrinks.
        ix = autoscale_pool(
            running=[1, 2, 3],
            online=[1, 2, 3],
            busy={3},
            jobs=[job("in_progress"), finished(runner="baml-r2-1")],
        )
        await run_reconcile(ix, AUTO_ENV | {"MIN_WARM": "2"})
        self.assertEqual(ix.stopped_by_run, ["baml-runner-2"])

    async def test_the_stop_cap_bounds_one_tick(self):
        ix = autoscale_pool(
            running=[1, 2, 3, 4, 5, 6], online=[1, 2, 3, 4, 5, 6], jobs=[finished()]
        )
        await run_reconcile(ix, AUTO_ENV | {"MAX_STOPS": "2"})
        self.assertEqual(ix.stopped_by_run, ["baml-runner-6", "baml-runner-5"])

    async def test_starts_are_not_capped(self):
        # Short of capacity is the state with a queue behind it: rationing a
        # start rations the queue.
        ix = autoscale_pool(
            running=[1], online=[1], stopped=[2, 3, 4, 5, 6], jobs=[job()] * 6
        )
        await run_reconcile(ix, AUTO_ENV | {"MAX_STOPS": "1"})
        self.assertEqual(len(ix.started), 5)

    async def test_a_wake_rotates_a_fresh_token_before_starting(self):
        # Scale-down deregistered the member, so it has nothing to reconnect
        # with: it must RE-register at boot, which it only does because the
        # token file changed. The write must be an overwrite, never a
        # delete-then-insert, or the platform fires no rotation and the
        # machine boots with the spent token it was created with.
        ix = autoscale_pool(
            running=[1], online=[1], stopped=[2, 3, 4, 5, 6], jobs=[job()] * 2
        )
        await run_reconcile(ix, AUTO_ENV)
        self.assertEqual(ix.started, ["baml-runner-2"])
        self.assertEqual(
            [c for _, c in ix.calls if c[0] == "secret-set"],
            [("secret-set", "baml_runner_reg_token", "REGTOKEN")],
        )
        self.assertEqual([c for _, c in ix.calls if c[0] == "secret-delete"], [])

    async def test_the_spent_secret_is_never_deleted(self):
        # Deleting it makes the NEXT write an insert, which fires no
        # rotation, which means no stopped member ever receives a usable
        # token again. The row is the rotation anchor.
        ix = autoscale_pool(running=[1], online=[1])
        await run_reconcile(ix, AUTO_ENV | {"POOL_SIZE": "2"})
        self.assertEqual(ix.created, ["baml-runner-2"])
        self.assertEqual([c for _, c in ix.calls if c[0] == "secret-delete"], [])


class IdleClockTest(unittest.IsolatedAsyncioTestCase):
    """Idle time is derived from GitHub's job timestamps - no stored state,
    no consecutive-tick counters, nothing that can get out of step."""

    async def test_a_recently_busy_member_is_inside_its_grace(self):
        ix = autoscale_pool(
            running=[1, 2],
            online=[1, 2],
            jobs=[
                finished(runner="baml-r2-1", ago=60),
                finished(runner="baml-r1-1", ago=90),
            ],
        )
        await run_reconcile(ix, AUTO_ENV | {"IDLE_GRACE_SECONDS": "600"})
        self.assertEqual(ix.stopped_by_run, [])

    async def test_a_long_idle_member_is_past_its_grace(self):
        ix = autoscale_pool(
            running=[1, 2],
            online=[1, 2],
            jobs=[finished(runner="baml-r2-1", ago=3600)],
        )
        await run_reconcile(ix, AUTO_ENV | {"IDLE_GRACE_SECONDS": "600"})
        self.assertEqual(ix.stopped_by_run, ["baml-runner-2"])

    async def test_an_idle_window_shorter_than_the_grace_proves_nothing(self):
        # A member absent from the scan is idle only back to the window's
        # own start. On a busy repo that window can be shorter than the
        # grace, and then absence is not evidence.
        ix = autoscale_pool(
            running=[1, 2],
            online=[1, 2],
            jobs=[finished(runner="baml-r1-1", ago=30)],
        )
        await run_reconcile(ix, AUTO_ENV | {"IDLE_GRACE_SECONDS": "600"})
        self.assertEqual(ix.stopped_by_run, [])

    async def test_no_completion_history_at_all_blocks_the_scale_down(self):
        ix = autoscale_pool(running=[1, 2], online=[1, 2])
        await run_reconcile(ix, AUTO_ENV)
        self.assertEqual(ix.stopped_by_run, [])


class ObservationFailureTest(unittest.IsolatedAsyncioTestCase):
    async def test_an_unreadable_queue_makes_no_scaling_decision(self):
        # Missing data is not zero demand and not zero idleness. Guessing up
        # costs money forever; guessing down stops machines about to be
        # handed a job. So: neither.
        for error in (
            http_error(502, "Bad gateway"),
            http_error(403, "Resource not accessible by integration"),
            urllib.error.URLError("connection reset"),
        ):
            with self.subTest(error=type(error).__name__):
                ix = autoscale_pool(
                    running=[1, 2, 3],
                    online=[1, 2, 3],
                    stopped=[4, 5, 6],
                    demand_error=error,
                )
                await run_reconcile(ix, AUTO_ENV)
                self.assertEqual(ix.started, [])
                self.assertEqual(ix.stopped_by_run, [])

    async def test_an_unreadable_queue_still_lets_the_pool_heal(self):
        # The scan runs before the execute phase, so an escaping error would
        # discard every create, replace and repair already decided on.
        ix = autoscale_pool(
            running=[1], online=[1], demand_error=http_error(502, "Bad gateway")
        )
        await run_reconcile(ix, AUTO_ENV | {"POOL_SIZE": "2"})
        self.assertEqual(ix.created, ["baml-runner-2"])


class OrderingTest(unittest.IsolatedAsyncioTestCase):
    """A partial tick must leave the pool BIGGER than intended, never
    smaller: too much capacity is a bill, too little is a stuck queue."""

    async def decision(self, ix, env):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            with (
                mock.patch.dict("os.environ", env, clear=True),
                mock.patch("reconcile.ix_runners.desired_rev", return_value=REV),
                mock.patch("reconcile.github.github_api", ix.github_api),
                mock.patch("reconcile.machines.create_options", lambda **kw: kw),
                mock.patch("reconcile.github.DEREGISTER_PAUSE", 0.0),
            ):
                await reconcile(ix)
        line = next(
            l for l in out.getvalue().splitlines() if l.startswith("DECISION ")
        )
        planned = line.split("| ", 1)[1]
        return planned.split("stop ")[0].strip(), "stop " + planned.split("stop ")[1]

    async def test_no_tick_ever_plans_a_start_and_a_stop_together(self):
        # The strongest form of the ordering rule: they are mutually
        # exclusive by construction (one if/elif on effective vs desired), so
        # a stop can never race a start in the first place. If this ever
        # fails, the two-phase execute is what keeps the invariant.
        scenarios = [
            # short of capacity, with idle members that look surplus
            dict(running=[1], online=[1], stopped=[2, 3], jobs=[job()] * 3),
            # long of capacity, with stopped members that could be woken
            dict(running=[1, 2, 3], online=[1, 2, 3], stopped=[4], jobs=[finished()]),
            # exactly at desired
            dict(running=[1], online=[1], stopped=[2, 3], jobs=[finished()]),
            # a config roll unsettling members while demand is real
            dict(running=[1, 2], online=[1, 2], stopped=[3], jobs=[job()] * 2),
        ]
        for kwargs in scenarios:
            with self.subTest(**{k: v for k, v in kwargs.items() if k != "jobs"}):
                starts, stops = await self.decision(
                    autoscale_pool(**kwargs), AUTO_ENV
                )
                self.assertFalse(
                    "[]" not in starts and "[]" not in stops,
                    f"planned both: {starts} / {stops}",
                )

    async def test_the_decision_line_records_the_whole_tick(self):
        # One line a reader can reconstruct the decision from, without
        # replaying the log.
        ix = autoscale_pool(
            running=[1], online=[1], stopped=[2, 3, 4, 5, 6], jobs=[job()] * 3
        )
        starts, stops = await self.decision(ix, AUTO_ENV)
        self.assertIn("[2, 3]", starts)
        self.assertIn("[]", stops)


class AutoscaleRegressionTest(unittest.IsolatedAsyncioTestCase):
    """Cases an earlier review found the first pass blind to. Several were
    live machine-destroying bugs, so each names what it prevents."""

    async def test_a_just_started_machine_is_never_deleted_for_being_silent(self):
        # THE bad one. A machine coming up reports Running immediately, but
        # its guest agent answers nothing for a few seconds - and the grace
        # covering that silent branch is measured from CREATION. A member
        # created last week and started twenty seconds ago sailed past it and
        # was deleted, taking its disk and its registration with it.
        ix = autoscale_pool(
            running=[1, 2],
            online=[1],
            info={
                "baml-runner-2": {
                    "created_at": now_ms() - DAY_MS,
                    "started_at": now_ms() - 20_000,
                }
            },
        )
        del ix.revs["baml-runner-2"]  # guest agent not answering yet
        await run_reconcile(ix, AUTO_ENV | {"POOL_SIZE": "2"})
        self.assertEqual([n for n, c in ix.calls if c == ("delete",)], [])
        self.assertEqual(ix.created, [])

    async def test_a_warming_machine_is_counted_so_no_second_one_is_started(self):
        ix = autoscale_pool(
            running=[1, 2],
            online=[1],
            stopped=[3, 4, 5, 6],
            jobs=[job()] * 2,
            info={"baml-runner-2": {"started_at": now_ms() - 20_000}},
        )
        await run_reconcile(ix, AUTO_ENV)
        self.assertEqual(ix.started, [])

    async def test_running_but_unhealthy_members_still_count_as_capacity(self):
        # A member deferred mid-config-roll is up and serving. Counting only
        # the healthy ones woke a parked machine for each one.
        ix = autoscale_pool(
            running=[1, 2, 3, 4],
            online=[1, 2, 3, 4],
            busy={1, 2, 3, 4},
            stopped=[5, 6],
            jobs=[job("in_progress")] * 4,
        )
        ix.revs = {name: OLD_REV for name in ix.revs}  # stale -> deferred, busy
        await run_reconcile(ix, AUTO_ENV)
        self.assertEqual(ix.started, [])

    async def test_refusing_to_scale_keeps_every_member_on(self):
        # The refusal logs "every member" - it has to mean it. A hand-set
        # MAX_ONLINE left in place kept stopping machines while announcing
        # that scaling was off.
        ix = autoscale_pool(running=[1, 2, 3, 4, 5, 6], online=[1, 2, 3, 4, 5, 6])
        env = AUTO_ENV | {"MIN_WARM": "2", "MAX_ONLINE": "4"}
        del env["RUNNER_LABEL"]
        with self.assertRaises(SystemExit):
            await run_reconcile(ix, env)
        self.assertEqual(ix.stopped_by_run, [])

    async def test_an_impossible_range_turns_scaling_off_loudly(self):
        ix = autoscale_pool(running=[1, 2], online=[1, 2])
        with self.assertRaises(SystemExit):
            await run_reconcile(ix, AUTO_ENV | {"MIN_WARM": "4", "MAX_ONLINE": "2"})
        self.assertEqual(ix.stopped_by_run, [])

    async def test_a_failed_machine_is_replaced_without_waiting_out_the_grace(self):
        # BOOT_GRACE exists because a young machine's SILENCE says nothing. A
        # machine reporting failed is not silent.
        ix = autoscale_pool(
            running=[1],
            online=[1],
            failed=[2],
            info={"baml-runner-2": {"created_at": now_ms() - 30_000}},
        )
        await run_reconcile(ix, AUTO_ENV | {"POOL_SIZE": "2"})
        self.assertIn(("baml-runner-2", ("delete",)), ix.calls)
        self.assertEqual(ix.created, ["baml-runner-2"])

    async def test_an_unknown_machine_status_is_skipped_not_deleted(self):
        # Every branch downstream reads a silent guest as delete-and-rebuild.
        ix = autoscale_pool(running=[1], online=[1])
        ix.vms.add("baml-runner-2")
        ix.info["baml-runner-2"] = {"status": "hibernating"}
        await run_reconcile(ix, AUTO_ENV | {"POOL_SIZE": "2"})
        self.assertEqual([n for n, c in ix.calls if c == ("delete",)], [])
        self.assertEqual(ix.created, [])

    async def test_a_hand_stopped_member_is_started_not_rebuilt(self):
        # Before autoscaling, a machine someone stopped read as unreachable
        # and was deleted and rebuilt. Starting it is the same outcome in
        # seconds instead of half an hour.
        ix = autoscale_pool(running=[1], online=[1], stopped=[2])
        await run_reconcile(ix, ENV | {"POOL_SIZE": "2"})
        self.assertEqual(ix.started, ["baml-runner-2"])
        self.assertEqual(ix.created, [])
        self.assertEqual([n for n, c in ix.calls if c == ("delete",)], [])

    async def test_the_job_queue_is_read_with_the_workflow_token_not_the_pat(self):
        # Listing workflow runs needs the Actions permission. The admin PAT
        # does not have it and must never be given it.
        ix = autoscale_pool(running=[1, 2], online=[1, 2], jobs=[job()])
        await run_reconcile(ix, AUTO_ENV)
        self.assertEqual(set(ix.demand_tokens), {"ghs_workflow_token"})
        self.assertNotIn(ENV["RUNNER_PAT"], ix.demand_tokens)


class DemandScanTest(unittest.TestCase):
    """The scan's arithmetic, away from the reconcile."""

    def scan(self, pages):
        seen = []

        def api(token, repo, path, *, method="GET", pat=True):
            seen.append(path)
            for prefix, body in pages.items():
                if path.startswith(prefix):
                    return body
            raise AssertionError(f"unexpected path {path}")

        with mock.patch("reconcile.github.github_api", api):
            return run_ids("tok", "example/baml", "queued", MAX_DEMAND_RUNS), seen

    def runs(self, n):
        return {"total_count": n, "workflow_runs": [{"id": i} for i in range(n)]}

    def test_exactly_the_cap_is_a_complete_answer(self):
        # An off-by-one here silently disables the demand signal at a round
        # number, and the pool sits at max forever.
        (ids, truncated), _ = self.scan(
            {"/actions/runs?status=queued": self.runs(MAX_DEMAND_RUNS)}
        )
        self.assertEqual(len(ids), MAX_DEMAND_RUNS)
        self.assertFalse(truncated)

    def test_one_past_the_cap_is_truncated(self):
        (ids, truncated), _ = self.scan(
            {"/actions/runs?status=queued": self.runs(MAX_DEMAND_RUNS + 1)}
        )
        self.assertTrue(truncated)

    def test_a_run_id_is_never_counted_twice(self):
        (ids, _), _ = self.scan({"/actions/runs?status=queued": self.runs(3)})
        self.assertEqual(len(ids), 3)


class LabelMatchTest(unittest.TestCase):
    def test_a_job_needs_every_label_on_one_runner(self):
        sets = [{"self-hosted", "ix", "ix-linux-x64"}]
        self.assertTrue(pool_can_serve({"labels": ["self-hosted", "ix"]}, sets))
        self.assertTrue(pool_can_serve({"labels": ["ix", "ix-linux-x64"]}, sets))
        self.assertFalse(pool_can_serve({"labels": ["ix", "gpu"]}, sets))
        self.assertFalse(pool_can_serve({"labels": ["blacksmith-4vcpu"]}, sets))

    def test_two_runners_cannot_combine_to_cover_one_job(self):
        # Labels are ANDed against a SINGLE runner. A pool with one "ix"
        # machine and one "gpu" machine cannot serve a job wanting both.
        sets = [{"self-hosted", "ix"}, {"self-hosted", "gpu"}]
        self.assertFalse(pool_can_serve({"labels": ["ix", "gpu"]}, sets))

    def test_a_job_with_no_labels_is_never_ours(self):
        self.assertFalse(pool_can_serve({"labels": []}, [{"self-hosted", "ix"}]))

    def test_label_sets_are_read_off_the_registrations(self):
        runners = [
            {
                "name": "baml-r1-1",
                "status": "online",
                "busy": False,
                "labels": [{"name": "self-hosted"}, {"name": "ix"}],
            },
            {"name": "other-r1-1", "status": "online", "busy": False,
             "labels": [{"name": "nope"}]},
        ]
        self.assertEqual(
            runner_label_sets(runners, "baml", 2), [{"self-hosted", "ix"}]
        )


class MachineStateTest(unittest.TestCase):
    def test_status_is_compared_case_insensitively(self):
        # MachineStatus is a lowercase StrEnum, but a record built anywhere
        # else can carry a plain string. One capital letter would read a
        # STOPPED member as running, probe it, get silence, and delete it.
        self.assertEqual(machine_status(FakeInfo("m", status="Stopped")), "stopped")
        self.assertEqual(
            machine_status(FakeInfo("m", status=FakeStatus.STOPPED)), "stopped"
        )
        self.assertEqual(machine_status(FakeInfo("m", status=None)), "")

    def test_slots_never_read_as_zero(self):
        # A bootstrap pool has no registrations, and demand divided by zero
        # slots ends the run.
        self.assertEqual(pool_slots([], "baml", 4), 1)

    def test_the_widest_member_sets_the_slot_count(self):
        runners = [
            {"name": "baml-r1-1", "status": "online", "busy": False},
            {"name": "baml-r2-1", "status": "online", "busy": False},
            {"name": "baml-r2-2", "status": "online", "busy": False},
        ]
        self.assertEqual(pool_slots(runners, "baml", 2), 2)

    def test_offline_registrations_count_toward_slots(self):
        runners = [
            {"name": "baml-r1-1", "status": "offline", "busy": False},
            {"name": "baml-r1-2", "status": "offline", "busy": False},
        ]
        self.assertEqual(pool_slots(runners, "baml", 1), 2)


class TimestampTest(unittest.TestCase):
    def test_github_timestamps_parse(self):
        self.assertAlmostEqual(
            parse_time("1970-01-01T00:01:00Z"), 60.0, delta=0.001
        )

    def test_a_bad_timestamp_is_none_not_an_exception(self):
        # These come off the network; one malformed value must not end a run.
        for bad in (None, "", "not-a-time", 17, "2026-13-45T99:99:99Z"):
            self.assertIsNone(parse_time(bad))


class ProbeMemberTest(unittest.IsolatedAsyncioTestCase):
    """The guest is the least trusted party in the system: it answers with
    whatever it likes, and nothing it says may end the run."""

    async def test_an_unforeseen_exception_reads_as_unreachable(self):
        # The old except tuple caught TimeoutError/OSError/RuntimeError only,
        # so a MemoryError from an oversized reply escaped and, through a
        # gather with no return_exceptions, cancelled every sibling probe.
        class Hostile:
            async def shell(self, script):
                raise MemoryError("the guest answered with gigabytes")

        self.assertEqual(await probe_member(Hostile(), clear_marker=False), (None, False))

    async def test_an_oversized_reply_is_not_trusted(self):
        # A reply that opens with a plausible rev and then floods reads
        # HEALTHY without a cap - the flood is the part that costs memory.
        class Flood:
            async def shell(self, script):
                return FakeExec(0, f"{REV}\n" + "x" * (MAX_PROBE_OUTPUT + 1))

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(
                await probe_member(Flood(), clear_marker=False), (None, False)
            )
        self.assertIn("::warning::", out.getvalue())


class ListRunnersTest(unittest.TestCase):
    def test_every_page_is_read(self):
        # Past 100 registrations an unpaginated listing reads the tail as
        # offline and mass-replaces the pool.
        rows = [
            {"id": i, "name": f"baml-r{i}-1", "status": "online", "busy": False}
            for i in range(1, 6)
        ]
        seen = []

        def api(pat, repo, path, *, method="GET"):
            seen.append(path)
            page = int(path.rsplit("page=", 1)[1])
            start = (page - 1) * 2
            return {"total_count": len(rows), "runners": rows[start : start + 2]}

        with mock.patch("reconcile.github.github_api", api):
            self.assertEqual(list_runners("pat", "example/baml"), rows)
        self.assertEqual(len(seen), 3)

    def test_a_short_listing_refuses_to_reconcile(self):
        def api(pat, repo, path, *, method="GET"):
            return {"total_count": 9, "runners": []}

        with mock.patch("reconcile.github.github_api", api):
            with self.assertRaises(SystemExit):
                list_runners("pat", "example/baml")

    def test_a_listing_without_total_count_is_refused(self):
        # total_count absent must not read as "complete": a full page with no
        # count would otherwise stop after page one and leave the tail
        # unlisted, and every unlisted member reads offline and is replaced.
        rows = [
            {"id": i, "name": f"baml-r{i}-1", "status": "online", "busy": False}
            for i in range(1, 4)
        ]

        def api(pat, repo, path, *, method="GET"):
            return {"runners": rows}

        with mock.patch("reconcile.github.github_api", api):
            with self.assertRaises(SystemExit):
                list_runners("pat", "example/baml")


class DesiredRevTest(unittest.TestCase):
    @staticmethod
    def fake_git(shallow: str, rev: str):
        def run(args, **kwargs):
            if args[1:3] == ["rev-parse", "--is-shallow-repository"]:
                return subprocess.CompletedProcess(args, 0, f"{shallow}\n", "")
            return subprocess.CompletedProcess(args, 0, f"{rev}\n", "")

        return run

    def test_a_shallow_checkout_is_refused(self):
        # A grafted HEAD diffs against the empty tree, so `git log -- <paths>`
        # names HEAD for every commit and the fleet rolls on every push.
        with mock.patch(
            "reconcile.config.subprocess.run", self.fake_git("true", REV)
        ):
            with self.assertRaises(SystemExit):
                desired_rev()

    def test_a_full_checkout_resolves_the_config_rev(self):
        with mock.patch(
            "reconcile.config.subprocess.run", self.fake_git("false", REV)
        ):
            self.assertEqual(desired_rev(), REV)

    def test_flake_dir_narrows_the_config_pathspec(self):
        # A subflake pool must roll ONLY on its own directory: the repo's
        # flake.nix and flake.lock are somebody else's concern by design.
        seen = []

        def run(args, **kwargs):
            if args[1:3] == ["rev-parse", "--is-shallow-repository"]:
                return subprocess.CompletedProcess(args, 0, "false\n", "")
            seen.append(args)
            return subprocess.CompletedProcess(args, 0, f"{REV}\n", "")

        with mock.patch("reconcile.config.subprocess.run", run):
            self.assertEqual(desired_rev("nix/ix"), REV)
        self.assertEqual(seen[0][seen[0].index("--") + 1 :], ["nix/ix/"])


class GithubApiTest(unittest.TestCase):
    """RUNNER_PAT carries Administration rw - repo takeover. Every rule here
    is about it never reaching a host that is not GitHub."""

    def test_the_base_is_pinned_whatever_the_environment_says(self):
        # $GITHUB_ENV lets any earlier step in the caller's job rewrite
        # GITHUB_API_URL, which would aim the Bearer PAT at its own host.
        with mock.patch.dict(
            "os.environ", {"GITHUB_API_URL": "https://evil.example"}, clear=True
        ):
            self.assertEqual(
                api_url("example/baml", "/actions/runners"),
                "https://api.github.com/repos/example/baml/actions/runners",
            )

    def test_ghes_opts_in_to_its_own_https_base(self):
        with mock.patch.dict(
            "os.environ",
            {
                "GITHUB_API_URL": "https://ghe.example/api/v3",
                "IX_RUNNERS_ALLOW_NON_HOSTED": "1",
            },
            clear=True,
        ):
            self.assertEqual(
                api_url("example/baml", "/x"),
                "https://ghe.example/api/v3/repos/example/baml/x",
            )

    def test_a_non_https_base_is_refused(self):
        out = io.StringIO()
        with (
            mock.patch.dict(
                "os.environ",
                {
                    "GITHUB_API_URL": "http://evil.example",
                    "IX_RUNNERS_ALLOW_NON_HOSTED": "1",
                },
                clear=True,
            ),
            contextlib.redirect_stdout(out),
        ):
            with self.assertRaises(SystemExit):
                api_url("example/baml", "/x")
        self.assertIn("::error::", out.getvalue())

    def test_every_call_goes_through_the_no_redirect_opener(self):
        # urlopen uses the default opener, which follows redirects: reaching
        # for it bypasses the refusal entirely.
        opener = mock.MagicMock()
        opener.open.return_value.__enter__.return_value.read.return_value = b"{}"
        with (
            mock.patch.dict("os.environ", {}, clear=True),
            mock.patch("reconcile.github.OPENER", opener),
        ):
            self.assertEqual(github_api("pat", "example/baml", "/x"), {})
        request = opener.open.call_args.args[0]
        self.assertEqual(
            request.full_url, "https://api.github.com/repos/example/baml/x"
        )

    def test_a_redirect_is_reported_as_a_refusal(self):
        opener = mock.MagicMock()
        opener.open.side_effect = http_error(302, "Found")
        with (
            mock.patch.dict("os.environ", {}, clear=True),
            mock.patch("reconcile.github.OPENER", opener),
        ):
            with self.assertRaises(RuntimeError) as caught:
                github_api("pat", "example/baml", "/x")
        self.assertIn("redirect", str(caught.exception))

    def test_a_redirect_is_never_followed_and_the_pat_is_not_resent(self):
        # urllib keeps the Authorization header across a 30x, unlike requests
        # and urllib3: one redirect would hand the PAT to the Location host.
        hits = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                hits.append(self.path)
                if self.path == "/redirect":
                    self.send_response(302)
                    self.send_header("Location", "/target")
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"{}")

            def log_message(self, *args):
                pass

        server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        request = urllib.request.Request(
            f"http://127.0.0.1:{server.server_port}/redirect",
            headers={"Authorization": "Bearer PAT"},
        )
        with self.assertRaises(urllib.error.HTTPError) as caught:
            OPENER.open(request, timeout=5)
        self.assertEqual(caught.exception.code, 302)
        self.assertEqual(hits, ["/redirect"])


class DeregisterTest(unittest.TestCase):
    @staticmethod
    def slots(member: int, count: int, busy=frozenset()):
        return [
            {
                "id": 1000 + member * 10 + slot,
                "name": f"baml-r{member}-{slot}",
                "status": "online",
                "busy": slot in busy,
            }
            for slot in range(1, count + 1)
        ]

    def test_a_busy_slot_defers_before_any_delete(self):
        # Deleting until a 422 stops us leaves a half-deregistered member that
        # still reads healthy and serves at reduced capacity.
        runners = self.slots(1, 4, busy={3})
        calls = []

        def api(pat, repo, path, *, method="GET"):
            calls.append((method, path))
            return {}

        with mock.patch("reconcile.github.github_api", api):
            self.assertFalse(deregister_member("pat", "r", runners, "baml", 1))
        self.assertEqual(calls, [])

    def test_a_mid_loop_busy_422_is_loud_about_the_damage(self):
        runners = self.slots(1, 3)

        def api(pat, repo, path, *, method="GET"):
            if path.endswith("1012"):
                raise http_error(422, "baml-r1-2 is still running a job")
            return {}

        out = io.StringIO()
        with (
            mock.patch("reconcile.github.github_api", api),
            mock.patch("reconcile.github.DEREGISTER_PAUSE", 0.0),
            contextlib.redirect_stdout(out),
        ):
            self.assertFalse(deregister_member("pat", "r", runners, "baml", 1))
        self.assertIn("::warning::", out.getvalue())
        self.assertIn("half-deregistered", out.getvalue())

    def test_a_non_busy_422_is_a_failure_not_a_defer(self):
        # A secondary-rate-limit 422 read as "busy" silently freezes every
        # replacement in the pool.
        runners = self.slots(1, 1)

        def api(pat, repo, path, *, method="GET"):
            raise http_error(422, "You have exceeded a secondary rate limit")

        with mock.patch("reconcile.github.github_api", api):
            with self.assertRaises(RuntimeError) as caught:
                deregister_member("pat", "r", runners, "baml", 1)
        # It raises (a failure, not a defer) and names the status and runner,
        # but never the authenticated response body (clear-text-logging).
        message = str(caught.exception)
        self.assertIn("HTTP 422", message)
        self.assertIn("baml-r1-1", message)
        self.assertNotIn("secondary rate limit", message)

    def test_a_404_registration_is_already_gone(self):
        runners = self.slots(1, 2)

        def api(pat, repo, path, *, method="GET"):
            raise http_error(404, "Not Found")

        with (
            mock.patch("reconcile.github.github_api", api),
            mock.patch("reconcile.github.DEREGISTER_PAUSE", 0.0),
        ):
            self.assertTrue(deregister_member("pat", "r", runners, "baml", 1))


class MemberMatchTest(unittest.TestCase):
    def test_prefix_does_not_cross_member_boundaries(self):
        runners = [{"name": "baml-r10-1", "status": "online"}]
        self.assertFalse(member_online(runners, "baml", 1))
        self.assertTrue(member_online(runners, "baml", 10))

    def test_offline_runner_does_not_count(self):
        runners = [{"name": "baml-r1-1", "status": "offline"}]
        self.assertFalse(member_online(runners, "baml", 1))

    def test_every_slot_of_a_member_is_matched(self):
        runners = [
            {"name": "baml-r1-1", "status": "online"},
            {"name": "baml-r1-2", "status": "offline"},
            {"name": "baml-r2-1", "status": "online"},
        ]
        self.assertEqual(len(member_runners(runners, "baml", 1)), 2)


class HostedRunnerTest(unittest.TestCase):
    """The hosted-only invariant is the whole trust model: this control plane
    holds IX_TOKEN and a repo-admin PAT and manages the machines a
    self-hosted runner would be."""

    def test_a_self_hosted_runner_is_refused(self):
        out = io.StringIO()
        with (
            mock.patch.dict("os.environ", {"RUNNER_ENVIRONMENT": "self-hosted"}, clear=True),
            contextlib.redirect_stdout(out),
        ):
            with self.assertRaises(SystemExit):
                require_hosted_runner()
        self.assertIn("::error::", out.getvalue())

    def test_an_absent_runner_environment_is_refused(self):
        # Fail closed: a runner that reports nothing is not a hosted one.
        with mock.patch.dict("os.environ", {}, clear=True):
            with self.assertRaises(SystemExit):
                require_hosted_runner()

    def test_a_hosted_runner_is_allowed(self):
        with mock.patch.dict(
            "os.environ", {"RUNNER_ENVIRONMENT": "github-hosted"}, clear=True
        ):
            require_hosted_runner()

    def test_ghes_can_opt_out_explicitly(self):
        with mock.patch.dict(
            "os.environ",
            {"RUNNER_ENVIRONMENT": "self-hosted", "IX_RUNNERS_ALLOW_NON_HOSTED": "1"},
            clear=True,
        ):
            require_hosted_runner()


class ConfigTest(unittest.TestCase):
    """The knob surface, resolved once and validated as a whole."""

    def load(self, env):
        with mock.patch.dict("os.environ", env, clear=True):
            return Config.load()

    def test_defaults_leave_autoscaling_off(self):
        # min_warm defaults to the pool size, so the floor meets the ceiling
        # and demand cannot move the answer. This is what makes adopting the
        # scaler a no-op until somebody dials it down.
        config = self.load(ENV | {"POOL_SIZE": "8"})
        self.assertEqual((config.min_warm, config.max_online), (8, 8))
        self.assertFalse(config.autoscaling)

    def test_a_refusal_is_what_turns_scaling_off(self):
        # `autoscaling` is DERIVED from the range rather than being a second
        # flag that has to be kept in step with it, so collapsing the range
        # is the whole of switching scaling off. If that ever stops being
        # true, a refused config keeps scaling with the operator's numbers.
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            config = self.load(
                {k: v for k, v in (AUTO_ENV | {"MIN_WARM": "2", "MAX_ONLINE": "4"}).items()
                 if k != "RUNNER_LABEL"}
            )
        self.assertTrue(config.refusals)
        self.assertFalse(config.autoscaling)
        self.assertEqual((config.min_warm, config.max_online), (6, 6))
        self.assertIn("::error::", out.getvalue())

    def test_an_impossible_range_is_refused_before_the_label_is_blamed(self):
        # Reporting "no RUNNER_LABEL" for a config whose real problem is an
        # inverted range sends the reader to the wrong knob.
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            config = self.load(AUTO_ENV | {"MIN_WARM": "4", "MAX_ONLINE": "2"})
        self.assertEqual(len(config.refusals), 1)
        self.assertIn("min-warm", config.refusals[0])

    def test_the_tick_mode_follows_the_trigger(self):
        for event, mode in [
            ("schedule", "scheduled"),
            ("workflow_dispatch", "scheduled"),
            ("workflow_run", "event"),
            ("push", "event"),
        ]:
            with self.subTest(event=event):
                self.assertEqual(
                    self.load(ENV | {"GITHUB_EVENT_NAME": event}).tick_mode, mode
                )

    def test_flake_dir_is_normalized_to_one_spelling(self):
        # The git pathspec and the template ref are both derived from this
        # value; two spellings of the same directory must not disagree.
        for raw, want in [
            ("nix/ix", "nix/ix"),
            ("./nix/ix", "nix/ix"),
            ("nix/ix/", "nix/ix"),
            ("", ""),
            (".", ""),
        ]:
            with self.subTest(raw=raw):
                self.assertEqual(
                    self.load(ENV | {"FLAKE_DIR": raw}).flake_dir, want
                )

    def test_a_flake_dir_outside_the_repo_is_refused(self):
        # The template ref pins github:<repo>/<rev>?dir=<flake-dir>; a
        # directory the ref cannot express must die here, not at create.
        for raw in ["/etc/nixos", "../elsewhere", "nix/../.."]:
            with self.subTest(raw=raw):
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    with self.assertRaises(SystemExit):
                        self.load(ENV | {"FLAKE_DIR": raw})

    def test_secrets_are_not_carried_on_the_config(self):
        # A dataclass has a repr and a repr ends up in tracebacks. The admin
        # PAT and the workflow token are passed separately for that reason.
        config = self.load(ENV)
        rendered = repr(config)
        self.assertNotIn(ENV["RUNNER_PAT"], rendered)
        self.assertNotIn(ENV["IX_TOKEN"], rendered)


class PoolSpecTest(unittest.TestCase):
    """One file defines the pool, and it is read strictly."""

    def toml(self, spec):
        """A dict as TOML. There is no stdlib writer, and the tests read
        better with dicts than with hand-written fragments."""

        def value(v):
            if isinstance(v, bool):
                return "true" if v else "false"
            return str(v) if isinstance(v, int) else json.dumps(v)

        return "\n".join(f"{k} = {value(v)}" for k, v in spec.items())

    def write(self, body):
        path = pathlib.Path(tempfile.mkdtemp()) / "ix-pool.toml"
        path.write_text(body if isinstance(body, str) else self.toml(body))
        return str(path)

    def load(self, body, env=None):
        path = self.write(body)
        out = io.StringIO()
        with (
            mock.patch.dict(
                "os.environ",
                dict(ENV, IX_POOL_SPEC=path,
                     GITHUB_TOKEN="ghs_workflow_token", **(env or {})),
                clear=True,
            ),
            contextlib.redirect_stdout(out),
        ):
            try:
                return Config.load(), out.getvalue()
            except SystemExit:
                return None, out.getvalue()

    def test_the_spec_supplies_the_pool(self):
        config, _ = self.load(
            {"pool-name": "demo", "region": "us-east-1", "pool-size": 12,
             "min-warm": 3, "runner-label": "ix"}
        )
        self.assertEqual(config.pool, "demo")
        self.assertEqual(config.regions, ("us-east-1",))
        self.assertEqual((config.pool_size, config.min_warm), (12, 3))
        self.assertTrue(config.autoscaling)

    def test_regions_spread_members_index_modulo(self):
        config, _ = self.load(
            {"pool-name": "demo", "regions": ["us-west-1", "us-east-1"],
             "pool-size": 32, "runner-label": "ix"}
        )
        self.assertEqual(config.regions, ("us-west-1", "us-east-1"))
        homes = [config.region_for(member) for member in range(1, 33)]
        # Even split, deterministically - not one region until it fills.
        self.assertEqual(homes.count("us-west-1"), 16)
        self.assertEqual(homes.count("us-east-1"), 16)
        self.assertNotEqual(homes[0], homes[1])
        # Failover is the NEXT region, and cycles.
        self.assertEqual(config.failover_region("us-west-1"), "us-east-1")
        self.assertEqual(config.failover_region("us-east-1"), "us-west-1")

    def test_a_single_region_pool_has_no_failover(self):
        config, _ = self.load(
            {"pool-name": "demo", "region": "us-east-1", "runner-label": "ix"}
        )
        self.assertEqual(config.region_for(7), "us-east-1")
        self.assertIsNone(config.failover_region("us-east-1"))

    def test_region_and_regions_together_refuse(self):
        config, out = self.load(
            {"pool-name": "demo", "region": "us-east-1",
             "regions": ["us-west-1"], "runner-label": "ix"}
        )
        self.assertIsNone(config)
        self.assertIn("both", out)

    def test_duplicate_regions_refuse(self):
        config, out = self.load(
            {"pool-name": "demo", "regions": ["us-east-1", "us-east-1"],
             "runner-label": "ix"}
        )
        self.assertIsNone(config)
        self.assertIn("repeats", out)

    def test_an_empty_or_nonstring_regions_list_refuses(self):
        for bad in ([], [1, 2], [""]):
            config, out = self.load(
                {"pool-name": "demo", "regions": bad, "runner-label": "ix"}
            )
            self.assertIsNone(config, bad)
            self.assertIn("regions", out)

    def test_regions_reach_the_config_from_the_environment_too(self):
        # The env path must parse the same shape (comma-separated), and it
        # must NOT be handed the single-region default the fixture carries -
        # that fed exactly the fallback prod lacked once before (#28).
        env = dict(ENV, GITHUB_TOKEN="ghs_workflow_token")
        env.pop("REGION")
        env["REGIONS"] = "us-west-1,us-east-1"
        with mock.patch.dict("os.environ", env, clear=True):
            config = Config.load()
        self.assertEqual(config.regions, ("us-west-1", "us-east-1"))

    def test_every_key_is_optional(self):
        # The file's PRESENCE declares that this repo has a pool; the values
        # all default in one place, so a minimal spec is a valid spec.
        config, _ = self.load({})
        self.assertEqual(config.pool_size, 8)
        self.assertFalse(config.autoscaling)

    def test_an_unknown_key_is_refused_not_ignored(self):
        # A typo that silently defaults is a pool quietly running someone
        # else's numbers: `mniWarm` would read as "autoscaling off" forever,
        # and the only symptom is the bill.
        config, out = self.load({"pool-size": 8, "mni-warm": 2})
        self.assertIsNone(config)
        self.assertIn("unknown key 'mni-warm'", out)

    def test_a_near_miss_names_the_key_it_meant(self):
        config, out = self.load({"minwarm": 2})
        self.assertIsNone(config)
        self.assertIn("did you mean 'min-warm'", out)

    def test_a_wrong_type_is_refused(self):
        config, out = self.load({"pool-size": "eight"})
        self.assertIsNone(config)
        self.assertIn("must be a whole number", out)

    def test_a_boolean_is_not_a_number(self):
        # bool subclasses int, so an isinstance check alone waves this
        # through and the pool gets a size of True.
        config, out = self.load({"pool-size": True})
        self.assertIsNone(config)
        self.assertIn("must be a whole number", out)

    def test_malformed_toml_is_refused_with_the_path(self):
        config, out = self.load("pool-size = = 8")
        self.assertIsNone(config)
        self.assertIn("could not be read as TOML", out)

    def test_comments_are_just_comments(self):
        # The whole reason for TOML over JSON: every key can say what it is,
        # in the file the operator actually edits.
        config, _ = self.load(
            "# the pool's name; VM names derive from it\n"
            'pool-name = "demo"\n'
            "pool-size = 4  # members, and the flake attrs mkPool generates\n"
        )
        self.assertEqual((config.pool, config.pool_size), ("demo", 4))

    def test_a_missing_spec_says_what_to_write(self):
        out = io.StringIO()
        with (
            mock.patch.dict(
                "os.environ", dict(ENV, IX_POOL_SPEC="/nonexistent/ix-pool.toml"), clear=True
            ),
            contextlib.redirect_stdout(out),
        ):
            with self.assertRaises(SystemExit):
                Config.load()
        self.assertIn("no pool spec at /nonexistent/ix-pool.toml", out.getvalue())
        self.assertIn("pool-name", out.getvalue())

    def test_the_spec_and_the_environment_agree_on_defaults(self):
        # Both entry points go through from_spec, so there is exactly one
        # implementation of every default and every rule. If they ever
        # diverge, a pool behaves differently in CI than under test.
        spec_config, _ = self.load(
            {"pool-size": 6, "min-warm": 2, "runner-label": "ix", "region": "us-east-1"}
        )
        with mock.patch.dict(
            "os.environ",
            dict(ENV, POOL_SIZE="6", MIN_WARM="2", RUNNER_LABEL="ix",
                 GITHUB_TOKEN="ghs_workflow_token"),
            clear=True,
        ):
            env_config = Config.load()
        self.assertEqual(
            dataclasses.astuple(spec_config), dataclasses.astuple(env_config)
        )

    def test_the_tick_mode_is_not_a_spec_key(self):
        # The trigger already says. Pinning it in a file would pin it for the
        # cron too, which is how a pool stops ever scaling down.
        self.assertNotIn("tick-mode", SPEC_KEYS)
        config, out = self.load({"tick-mode": "scheduled"})
        self.assertIsNone(config)
        self.assertIn("unknown key 'tick-mode'", out)


class ActionSurfaceTest(unittest.TestCase):
    """The action's declared surface and the script's own defaults are two
    files that have to agree, which is the exact shape of problem the pool
    spec exists to remove - so it should not reappear between them."""

    def action(self):
        root = pathlib.Path(__file__).resolve().parent.parent.parent
        return (root / "action.yml").read_text()

    def test_the_actions_default_spec_path_matches_the_scripts(self):
        # Parsed with a regex rather than a YAML library on purpose: the
        # suite runs with no third-party dependencies, which is what lets it
        # gate a job that holds a repo-admin PAT.
        block = re.search(r"\n  config-file:\n(?:.*\n)*?    default: (\S+)", self.action())
        self.assertIsNotNone(block, "config-file input lost its default")
        self.assertEqual(block.group(1), DEFAULT_SPEC_PATH)

    def test_only_the_ix_token_is_a_required_input(self):
        # The surface is the product here. A new required input is a change
        # every consumer has to make, so it should be deliberate. GitHub
        # auth is no longer required-by-declaration because there are two
        # ways to supply it; the action checks that itself, in a step that
        # names both.
        required = re.findall(r"\n  ([a-z-]+):\n(?:.*\n)*?    required: true", self.action())
        self.assertEqual(sorted(required), ["ix-token"])

    def test_no_github_app_private_key_is_ever_accepted(self):
        # An App's private key is APP-GLOBAL: it mints installation tokens
        # for EVERY installation of that App. Accepting one here would mean
        # asking a customer to put a credential for other people's pools in
        # their repo. There is no safe version of that, so the input does
        # not exist and no step consumes one.
        text = self.action()
        self.assertNotIn("private-key", text)
        self.assertNotIn("create-github-app-token", text)

    def test_every_third_party_action_is_pinned_by_sha(self):
        # Same reasoning for all of them: this job holds IX_TOKEN and a
        # credential that administers the pool.
        for used in re.findall(r"uses: (\S+)", self.action()):
            with self.subTest(uses=used):
                self.assertRegex(used, r"@[0-9a-f]{40}$")

    def test_the_admin_credential_never_transits_step_plumbing(self):
        # The credential is acquired in-process (reconcile/vending.py). The
        # action passes the caller's CHOICE and the raw PAT input through
        # env and nothing else: a step output would put a live credential in
        # the runner's inter-step plumbing, which is the design this
        # replaced.
        text = self.action()
        self.assertNotIn("steps.vend", text)
        self.assertNotIn("GITHUB_OUTPUT", text)
        env = re.search(r"GITHUB_ADMIN_TOKEN: (.+)", text).group(1)
        self.assertEqual(env.strip(), "${{ inputs.runner-pat }}")
        source = re.search(r"IX_TOKEN_SOURCE: (.+)", text).group(1)
        self.assertEqual(source.strip(), "${{ inputs.token-source }}")


class AdminCredentialTest(unittest.TestCase):
    """Which credential the reconcile administers runners with."""

    def resolve(self, env):
        with mock.patch.dict("os.environ", env, clear=True):
            return vending._legacy_pat()

    def test_the_app_token_is_preferred(self):
        self.assertEqual(
            self.resolve({"GITHUB_ADMIN_TOKEN": "ghs_app", "RUNNER_PAT": "github_pat"}),
            "ghs_app",
        )

    def test_the_legacy_pat_still_works_alone(self):
        # A workflow pinned to an older action rev sets only this one, and
        # must keep working through the deprecation window.
        self.assertEqual(self.resolve({"RUNNER_PAT": "github_pat"}), "github_pat")

    def test_an_empty_value_is_not_a_credential(self):
        # Actions sets an unprovided input to the empty string rather than
        # leaving it unset, so "is it present" is the wrong question.
        self.assertEqual(
            self.resolve({"GITHUB_ADMIN_TOKEN": "", "RUNNER_PAT": "github_pat"}),
            "github_pat",
        )

    def test_no_credential_at_all_is_refused_by_name(self):
        with self.assertRaises(SystemExit) as raised:
            self.resolve({})
        self.assertIn("GITHUB_ADMIN_TOKEN", str(raised.exception))
        self.assertIn("RUNNER_PAT", str(raised.exception))

    def test_main_refuses_before_doing_anything(self):
        # Failing at first use would mean failing after machines had already
        # been created this run.
        # sys.stdout is mocked rather than redirected because main()
        # reconfigures it for line buffering before doing anything else.
        with (
            mock.patch.dict(
                "os.environ",
                {"RUNNER_ENVIRONMENT": "github-hosted", "IX_TOKEN": "t",
                 "GITHUB_REPOSITORY": "o/r"},
                clear=True,
            ),
            mock.patch("reconcile.ix_runners.sys.stdout", mock.Mock()),
            mock.patch("reconcile.ix_runners.client", lambda: object()),
        ):
            with self.assertRaises(SystemExit) as raised:
                main()
        # The default source is the PAT era, so the preflight refusal names
        # the input to set and the mode to migrate to.
        self.assertIn("runner-pat", str(raised.exception))


class EntrypointTest(unittest.TestCase):
    """The file the composite action actually runs.

    Nothing else in this suite touches it: the tests import the package
    directly, so a broken entrypoint would be invisible here and visible in
    production, holding a repo-admin PAT. It resolves the package by putting
    its own PARENT on sys.path, which is exactly the kind of thing that
    works from the repo root and fails from anywhere else - so this runs it
    from an unrelated directory, as Actions does.
    """

    def run_entrypoint(self, cwd, env):
        script = pathlib.Path(__file__).resolve().parent.parent / "ix-runners"
        return subprocess.run(
            [sys.executable, str(script)],
            cwd=cwd,
            env={"PATH": os.environ.get("PATH", ""), **env},
            capture_output=True,
            text=True,
        )

    def test_the_entrypoint_imports_and_reaches_main(self):
        # Refusing a non-hosted runner is the FIRST thing main does, so
        # seeing that refusal proves the whole import chain resolved and
        # main() ran - without needing the SDK, which is x86_64-only.
        for cwd in ("/", tempfile.gettempdir()):
            with self.subTest(cwd=cwd):
                done = self.run_entrypoint(cwd, {})
                self.assertIn("not 'github-hosted'", done.stdout)

    def test_the_entrypoint_gets_past_the_gate_with_a_hosted_runner(self):
        # One step further in, so a package that imports but is missing a
        # name cannot pass the test above by refusing early.
        done = self.run_entrypoint("/", {"RUNNER_ENVIRONMENT": "github-hosted"})
        self.assertIn("IX_TOKEN is required", done.stdout + done.stderr)


class MainTest(unittest.TestCase):
    def test_stdout_is_line_buffered(self):
        # Unbuffered ordering is a security property here: log lines and the
        # ::add-mask:: must reach the runner before the stderr they explain.
        stdout = mock.Mock()

        async def converged(ix):
            return 0

        with (
            mock.patch.dict(
                "os.environ", dict(ENV, RUNNER_ENVIRONMENT="github-hosted"), clear=True
            ),
            mock.patch("reconcile.ix_runners.sys.stdout", stdout),
            mock.patch("reconcile.ix_runners.client", lambda: object()),
            mock.patch("reconcile.ix_runners.reconcile", converged),
        ):
            main()
        stdout.reconfigure.assert_called_once_with(line_buffering=True)


class ExtraMembersTest(unittest.TestCase):
    def test_only_members_above_the_pool_size_are_extra(self):
        names = ["baml-runner-1", "baml-runner-9", "baml-runner-10", "other-runner-3"]
        self.assertEqual(extra_members(names, "baml", 8), [9, 10])

    def test_a_foreign_name_is_never_pruned(self):
        self.assertEqual(extra_members(["baml-runner-x", "bamlx-runner-9"], "baml", 1), [])


if __name__ == "__main__":
    unittest.main()
