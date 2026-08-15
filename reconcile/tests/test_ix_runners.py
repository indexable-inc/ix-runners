from __future__ import annotations

import asyncio
import contextlib
import email.message
import enum
import http.server
import io
import json
import os
import pathlib
import subprocess
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from unittest import mock

from reconcile.ix_runners import (
    IDLE_PATH,
    MAX_DEMAND_RUNS,
    MAX_PROBE_OUTPUT,
    OPENER,
    api_url,
    deregister_member,
    desired_rev,
    extra_members,
    github_api,
    list_runners,
    main,
    member_online,
    member_runners,
    probe_member,
    reconcile,
    require_hosted_runner,
)

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
    "IX_REGION": "us-east-1",
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
        # The real probe always prints the count, falling back to 0, so a
        # machine built before the file existed answers 0 rather than nothing.
        out += f"idle={self.platform.idle.get(self.name, 0)}\n"
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
        idle=None,
        jobs=None,
        busy_at_stop=frozenset(),
        demand_error=None,
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
        self.idle = dict(idle or {})  # name -> consecutive-idle count on the VM
        # (labels, status) per active job, the shape the demand scan counts.
        self.jobs = list(jobs or [])
        # members that take a job between the scan and the stop: idle in the
        # first listing, busy in the fresh one the stop path re-reads.
        self.busy_at_stop = set(busy_at_stop)
        self.runner_listings = 0
        self.demand_tokens = []  # which credential read the job queue
        self.demand_error = demand_error
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
            # The stop path re-lists moments before pulling the power; from
            # the second listing on, busy_at_stop members are mid-job.
            busy = self.busy | (
                self.busy_at_stop if self.runner_listings > 1 else set()
            )
            rows = self.runner_rows(busy)
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
            # One run per active job, which is the worst case for the scan.
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
            mock.patch("reconcile.ix_runners.github_api", ix.github_api),
            mock.patch("reconcile.ix_runners.create_options", lambda **kw: kw),
            mock.patch("reconcile.ix_runners.DEREGISTER_PAUSE", 0.0),
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

    async def test_registration_token_is_masked_and_the_secret_cleaned_up(self):
        # The token can register a job-stealing runner for an hour: it must
        # never land unmasked in a log, and must not outlive the run.
        ix = FakeIx(vms=set(), revs={}, online=set(), markers=set())
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            await self.reconcile_with(ix)
        self.assertIn("::add-mask::REGTOKEN", out.getvalue())
        self.assertIn((None, ("secret-delete", "baml_runner_reg_token")), ix.calls)

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
        with mock.patch("reconcile.ix_runners.probe_member") as probe:
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
}


def job(status="queued", label="ix"):
    """One active job, in the shape the demand scan reads: `labels` is the
    job's `runs-on` set."""
    return {"status": status, "labels": ["self-hosted", label]}


def autoscale_pool(*, running=(), stopped=(), failed=(), online=(), **kwargs):
    """A pool whose members are in the given power states.

    Every running member is on the current rev. Only running members get a
    rev at all: a stopped one that gets probed anyway raises out of the fake
    guest, reads as unreachable, and is replaced - which is precisely the
    failure these tests exist to catch, so it must not be papered over here.

    Every member that exists is registered on GitHub, stopped ones included:
    a stop keeps the disk, so the registration credentials survive and the
    runner shows up offline rather than vanishing.
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
    return FakeIx(
        vms=names(running) | names(stopped) | names(failed),
        revs={f"baml-runner-{m}": REV for m in running},
        online=set(online),
        markers=set(),
        registered=set(running) | set(stopped) | set(failed),
        info=info,
        **kwargs,
    )


async def run_reconcile(ix, env):
    with (
        mock.patch.dict("os.environ", env, clear=True),
        mock.patch("reconcile.ix_runners.desired_rev", return_value=REV),
        mock.patch("reconcile.ix_runners.github_api", ix.github_api),
        mock.patch("reconcile.ix_runners.create_options", lambda **kw: kw),
        mock.patch("reconcile.ix_runners.DEREGISTER_PAUSE", 0.0),
        contextlib.redirect_stdout(io.StringIO()),
    ):
        return await reconcile(ix)


class AutoscaleTest(unittest.IsolatedAsyncioTestCase):
    """Power state is the only dynamic axis: the member set is declarative,
    and every test here is about which of those members are switched on."""

    async def test_an_unconfigured_pool_never_scales_and_never_asks_demand(self):
        # MIN_WARM defaults to POOL_SIZE, so desired is pinned to the whole
        # pool and no clamp can move it. Asking GitHub for a demand number
        # that cannot change the answer is a wasted scan on every tick, and
        # it would make RUNNER_LABEL mandatory for pools that never scale.
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
        probed = {name for name, call in ix.calls if call[0] == "shell"}
        self.assertEqual(probed, {"baml-runner-1"})
        self.assertEqual([c for _, c in ix.calls if c[0] == "create"], [])
        self.assertEqual([n for n, c in ix.calls if c == ("delete",)], [])
        self.assertEqual(ix.started, [])
        self.assertEqual(ix.stopped_by_run, [])

    async def test_a_surplus_stops_the_highest_indexed_idle_members(self):
        # Highest index first, so the warm core is always the same low
        # members and their template and toolchain caches stay hot.
        ix = autoscale_pool(running=[1, 2, 3, 4], online=[1, 2, 3, 4])
        await run_reconcile(ix, AUTO_ENV)
        self.assertEqual(
            ix.stopped_by_run, ["baml-runner-4", "baml-runner-3", "baml-runner-2"]
        )

    async def test_a_busy_member_is_passed_over_for_the_next_idle_one(self):
        # Two layers protect a running job, and this pins the FIRST one: the
        # busy member is never even planned for a stop. Asserting only that
        # it stays up proves nothing, because the execute-time re-read would
        # catch it anyway - and then the scale-down slot is spent on a member
        # that was never stoppable, so the pool never actually shrinks.
        # Member 3 is the highest index, so it is the one the stop order
        # reaches for first.
        ix = autoscale_pool(
            running=[1, 2, 3], online=[1, 2, 3], busy={3}, jobs=[job("in_progress")]
        )
        await run_reconcile(ix, AUTO_ENV | {"MIN_WARM": "2"})
        self.assertEqual(ix.stopped_by_run, ["baml-runner-2"])

    async def test_min_warm_is_a_floor_under_the_scale_down(self):
        ix = autoscale_pool(running=[1, 2, 3], online=[1, 2, 3])
        await run_reconcile(ix, AUTO_ENV | {"MIN_WARM": "2"})
        self.assertEqual(ix.stopped_by_run, ["baml-runner-3"])

    async def test_scale_up_starts_a_stopped_member_rather_than_creating(self):
        # A start is seconds; a create is a template build. Preferring the
        # start is most of the latency story.
        ix = autoscale_pool(
            running=[1], online=[1], stopped=[2, 3, 4, 5, 6], jobs=[job()] * 3
        )
        await run_reconcile(ix, AUTO_ENV)
        self.assertEqual(ix.started, ["baml-runner-2", "baml-runner-3"])
        self.assertEqual(ix.created, [])

    async def test_a_warming_member_counts_as_online_and_is_left_alone(self):
        # A machine reports Running the moment it comes up, so a member
        # started seconds ago looks exactly like a healthy one whose runners
        # died. Repairing it restarts units mid-registration, and not
        # counting it starts a second machine for the same job.
        ix = autoscale_pool(
            running=[1, 2],
            online=[1],
            jobs=[job()] * 2,
            info={"baml-runner-2": {"started_at": now_ms() - 10_000}},
        )
        await run_reconcile(ix, AUTO_ENV)
        self.assertEqual(ix.started, [])
        self.assertEqual(ix.stopped_by_run, [])
        self.assertNotIn(
            ("baml-runner-2", ("systemctl", "restart", "github-runner-*")), ix.calls
        )

    async def test_a_failed_machine_is_replaced_without_waiting_out_the_grace(self):
        # BOOT_GRACE exists because a young machine's SILENCE says nothing.
        # A machine reporting failed is not silent - it has said it is dead -
        # so waiting the grace out is half an hour of a member that is never
        # coming back.
        ix = autoscale_pool(
            running=[1],
            online=[1],
            failed=[2],
            info={"baml-runner-2": {"created_at": now_ms() - 30_000}},
        )
        await run_reconcile(ix, AUTO_ENV | {"POOL_SIZE": "2"})
        self.assertIn(("baml-runner-2", ("delete",)), ix.calls)
        self.assertEqual(ix.created, ["baml-runner-2"])

    async def test_the_power_cap_bounds_one_run(self):
        # A thundering herd of stops is as bad as one of creates: the cap
        # makes a wrong scaling decision converge slowly instead of at once.
        ix = autoscale_pool(running=[1, 2, 3, 4, 5, 6], online=[1, 2, 3, 4, 5, 6])
        await run_reconcile(ix, AUTO_ENV | {"MAX_POWER_ACTIONS": "2"})
        self.assertEqual(ix.stopped_by_run, ["baml-runner-6", "baml-runner-5"])

    async def test_a_member_that_takes_a_job_before_the_stop_is_left_running(self):
        # The scan snapshot is tens of seconds old by execute time, and
        # nothing on GitHub's side locks a stop the way a 422 locks a
        # deregister. Re-reading the registrations right before pulling the
        # power is what narrows the window.
        ix = autoscale_pool(running=[1, 2], online=[1, 2], busy_at_stop={2})
        await run_reconcile(ix, AUTO_ENV)
        self.assertEqual(ix.stopped_by_run, [])

    async def test_idle_grace_counts_consecutive_scans_on_the_vm(self):
        # First scan: idle, but one short of the grace, so the count is
        # written to the machine rather than the power pulled.
        ix = autoscale_pool(running=[1, 2], online=[1, 2])
        await run_reconcile(ix, AUTO_ENV | {"IDLE_GRACE_TICKS": "2"})
        self.assertEqual(ix.stopped_by_run, [])
        self.assertIn(
            (
                "baml-runner-2",
                ("sh", "-c", "mkdir -p /var/lib/ix-runner && echo 1 > " + IDLE_PATH),
            ),
            ix.calls,
        )

    async def test_idle_grace_stops_once_the_count_is_reached(self):
        ix = autoscale_pool(
            running=[1, 2], online=[1, 2], idle={"baml-runner-2": 1}
        )
        await run_reconcile(ix, AUTO_ENV | {"IDLE_GRACE_TICKS": "2"})
        self.assertEqual(ix.stopped_by_run, ["baml-runner-2"])

    async def test_a_member_that_took_a_job_has_its_idle_count_reset(self):
        # Grace means CONSECUTIVE idle scans. Without the reset a machine
        # that went busy and idle again over many scans accumulates its way
        # to a stop it never earned.
        ix = autoscale_pool(
            running=[1, 2],
            online=[1, 2],
            busy={2},
            idle={"baml-runner-2": 1},
            jobs=[job("in_progress")] * 2,
        )
        await run_reconcile(ix, AUTO_ENV | {"IDLE_GRACE_TICKS": "3"})
        self.assertEqual(ix.stopped_by_run, [])
        self.assertIn(
            (
                "baml-runner-2",
                ("sh", "-c", "mkdir -p /var/lib/ix-runner && echo 0 > " + IDLE_PATH),
            ),
            ix.calls,
        )

    async def test_demand_counts_only_jobs_targeting_this_pools_label(self):
        # A repo's other jobs run on GitHub-hosted runners and must not size
        # this pool: counting them would keep the whole fleet warm for work
        # that never lands on it.
        ix = autoscale_pool(
            running=[1],
            online=[1],
            stopped=[2, 3, 4, 5, 6],
            jobs=[job()] * 3 + [job(label="ubuntu-latest")] * 5,
        )
        await run_reconcile(ix, AUTO_ENV)
        self.assertEqual(ix.started, ["baml-runner-2", "baml-runner-3"])

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

    async def test_slots_divide_jobs_into_members(self):
        # Demand is jobs; the pool is machines. With four slots per machine,
        # eight jobs need two machines, not eight.
        ix = autoscale_pool(
            running=[1], online=[1], stopped=[2, 3, 4, 5, 6], slots=4, jobs=[job()] * 8
        )
        await run_reconcile(ix, AUTO_ENV)
        self.assertEqual(ix.started, ["baml-runner-2"])

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

    async def test_an_untrustworthy_demand_number_scales_up_not_down(self):
        # Past the cap the count is not to be believed, and the safe
        # direction is up: guessing low strands a queue behind a parked pool.
        ix = autoscale_pool(
            running=[1],
            online=[1],
            stopped=[2, 3, 4, 5, 6],
            jobs=[job()] * (MAX_DEMAND_RUNS + 1),
        )
        await run_reconcile(ix, AUTO_ENV | {"MAX_POWER_ACTIONS": "99"})
        self.assertEqual(len(ix.started), 5)

    async def test_the_job_queue_is_read_with_the_workflow_token_not_the_pat(self):
        # Listing workflow runs needs the Actions permission. The admin PAT
        # does not have it and must never be given it: repo administration
        # is registration-token minting and runner deletion, and a demand
        # scan is neither.
        ix = autoscale_pool(running=[1, 2], online=[1, 2], jobs=[job()])
        await run_reconcile(ix, AUTO_ENV)
        self.assertEqual(set(ix.demand_tokens), {"ghs_workflow_token"})
        self.assertNotIn(ENV["RUNNER_PAT"], ix.demand_tokens)

    async def test_a_forbidden_demand_read_scales_up_rather_than_dying(self):
        # A token missing `actions: read` must not stop the pool being
        # reconciled, and must not park it either: no view of the queue means
        # keep the machines on.
        ix = autoscale_pool(
            running=[1],
            online=[1],
            stopped=[2, 3, 4, 5, 6],
            demand_error=http_error(403, "Resource not accessible by integration"),
        )
        await run_reconcile(ix, AUTO_ENV | {"MAX_POWER_ACTIONS": "99"})
        self.assertEqual(len(ix.started), 5)

    async def test_a_missing_label_turns_scaling_off_loudly_but_still_heals(self):
        # A broken scaling config must not stop the pool being repaired: a
        # pool nobody heals is worse than a pool that is briefly too big. So
        # the run goes red, scaling is off, and the replace still happens.
        ix = autoscale_pool(running=[1, 2], online=[1], stopped=[3])
        del ix.revs["baml-runner-2"]  # unreachable -> replace
        env = AUTO_ENV | {"MIN_WARM": "1", "POOL_SIZE": "3"}
        del env["RUNNER_LABEL"]
        with self.assertRaises(SystemExit):
            await run_reconcile(ix, env)
        self.assertEqual(ix.created, ["baml-runner-2"])
        # Scaling off means desired is the whole pool, so the parked member
        # is switched back on rather than left dark.
        self.assertEqual(ix.started, ["baml-runner-3"])
        self.assertEqual(ix.stopped_by_run, [])

    async def test_an_impossible_range_turns_scaling_off_loudly(self):
        ix = autoscale_pool(running=[1, 2], online=[1, 2])
        with self.assertRaises(SystemExit):
            await run_reconcile(ix, AUTO_ENV | {"MIN_WARM": "4", "MAX_ONLINE": "2"})
        self.assertEqual(ix.stopped_by_run, [])

    async def test_a_hand_stopped_member_is_started_not_rebuilt(self):
        # Before autoscaling, a machine someone stopped read as unreachable
        # and was deleted and rebuilt from its template. Starting it is the
        # same outcome in seconds instead of half an hour.
        ix = autoscale_pool(running=[1], online=[1], stopped=[2])
        await run_reconcile(ix, ENV | {"POOL_SIZE": "2"})
        self.assertEqual(ix.started, ["baml-runner-2"])
        self.assertEqual(ix.created, [])
        self.assertEqual([n for n, c in ix.calls if c == ("delete",)], [])


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

        self.assertEqual(
            await probe_member(Hostile(), clear_marker=False), (None, False, 0)
        )

    async def test_an_oversized_reply_is_not_trusted(self):
        # A reply that opens with a plausible rev and then floods reads
        # HEALTHY without a cap - the flood is the part that costs memory.
        class Flood:
            async def shell(self, script):
                return FakeExec(0, f"{REV}\n" + "x" * (MAX_PROBE_OUTPUT + 1))

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(
                await probe_member(Flood(), clear_marker=False), (None, False, 0)
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

        with mock.patch("reconcile.ix_runners.github_api", api):
            self.assertEqual(list_runners("pat", "example/baml"), rows)
        self.assertEqual(len(seen), 3)

    def test_a_short_listing_refuses_to_reconcile(self):
        def api(pat, repo, path, *, method="GET"):
            return {"total_count": 9, "runners": []}

        with mock.patch("reconcile.ix_runners.github_api", api):
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
            "reconcile.ix_runners.subprocess.run", self.fake_git("true", REV)
        ):
            with self.assertRaises(SystemExit):
                desired_rev()

    def test_a_full_checkout_resolves_the_config_rev(self):
        with mock.patch(
            "reconcile.ix_runners.subprocess.run", self.fake_git("false", REV)
        ):
            self.assertEqual(desired_rev(), REV)


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
            mock.patch("reconcile.ix_runners.OPENER", opener),
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
            mock.patch("reconcile.ix_runners.OPENER", opener),
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

        with mock.patch("reconcile.ix_runners.github_api", api):
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
            mock.patch("reconcile.ix_runners.github_api", api),
            mock.patch("reconcile.ix_runners.DEREGISTER_PAUSE", 0.0),
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

        with mock.patch("reconcile.ix_runners.github_api", api):
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
            mock.patch("reconcile.ix_runners.github_api", api),
            mock.patch("reconcile.ix_runners.DEREGISTER_PAUSE", 0.0),
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
