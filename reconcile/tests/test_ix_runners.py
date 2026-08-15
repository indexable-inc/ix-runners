from __future__ import annotations

import asyncio
import contextlib
import email.message
import io
import json
import subprocess
import time
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

from reconcile.ix_runners import (
    deregister_member,
    desired_rev,
    list_runners,
    member_online,
    member_runners,
    reconcile,
)

ROOT = Path(__file__).resolve().parents[2]

REV = "a" * 40
OLD_REV = "b" * 40


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

ENV = {
    "IX_TOKEN": "ix_test_token",
    "RUNNER_PAT": "github_pat_test",
    "GITHUB_REPOSITORY": "example/baml",
    "POOL_SIZE": "2",
    "MAX_REPLACEMENTS": "2",
    "IX_REGION": "us-east-1",
}


class FakeInfo:
    def __init__(self, name):
        self.name = name
        self.id = f"id-{name}"


class FakeExec:
    """Mirrors the SDK's ExecResult: exit_code is a PROPERTY, not a method
    (calling it was a live failure the method-shaped fake waved through)."""

    def __init__(self, code):
        self._code = code

    @property
    def exit_code(self):
        return self._code


class FakeMachine:
    """One VM: scripted rev/marker state; records guest commands."""

    def __init__(self, platform, name):
        self.platform = platform
        self.name = name

    async def read_file(self, path):
        if self.name not in self.platform.revs:
            raise OSError("unreachable")
        return self.platform.revs[self.name] + "\n"

    async def exec(self, command):
        self.platform.calls.append((self.name, tuple(command)))
        if command[0] == "test":
            return FakeExec(0 if self.name in self.platform.markers else 1)
        return FakeExec(0)

    async def delete(self):
        self.platform.calls.append((self.name, ("delete",)))


class FakeMachines:
    def __init__(self, platform):
        self.platform = platform

    async def list(self):
        return [FakeInfo(name) for name in sorted(self.platform.vms)]

    def connect(self, vm_id):
        name = vm_id.removeprefix("id-")
        return FakeMachine(self.platform, name)

    async def create(self, options):
        self.platform.calls.append((None, ("create", options)))
        if self.platform.broken:
            raise RuntimeError("template build failed")
        self.platform.in_flight += 1
        self.platform.max_in_flight = max(
            self.platform.max_in_flight, self.platform.in_flight
        )
        await asyncio.sleep(self.platform.create_delay)
        self.platform.in_flight -= 1


class FakeSecrets:
    def __init__(self, platform):
        self.platform = platform

    async def set(self, name, value):
        self.platform.calls.append((None, ("secret-set", name, value)))


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
        broken=False,
        page_size=100,
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
        self.registered = set(
            self.online | self.busy if registered is None else registered
        )
        self.slots = slots  # runner daemons per VM
        self.broken = broken  # every create fails (bad template rev)
        self.page_size = page_size  # runner listing page size
        self.calls = []
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

    def runner_rows(self):
        return [
            {
                "id": 1000 + member * 10 + slot,
                "name": f"baml-r{member}-{slot}",
                "status": "online" if member in self.online else "offline",
                "busy": member in self.busy,
            }
            for member in sorted(self.registered)
            for slot in range(1, self.slots + 1)
        ]

    def github_api(self, pat, repo, path, *, method="GET"):
        self.calls.append((None, (method, path)))
        if path.startswith("/actions/runners?"):
            rows = self.runner_rows()
            start = (int(path.rsplit("page=", 1)[1]) - 1) * self.page_size
            return {
                "total_count": len(rows),
                "runners": rows[start : start + self.page_size],
            }
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
            mock.patch.dict("os.environ", env),
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

    async def test_unreachable_member_is_replaced(self):
        ix = FakeIx(
            vms={"baml-runner-1", "baml-runner-2"},
            revs={"baml-runner-2": REV},
            online={2},
            markers=set(),
        )
        self.assertEqual(await self.reconcile_with(ix), 1)
        self.assertIn(("baml-runner-1", ("delete",)), ix.calls)

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
        self.assertIn(
            ("baml-runner-1", ("touch", "/var/lib/ix-runner/repaired")),
            ix.calls,
        )
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

    async def test_healthy_member_clears_the_strike_marker(self):
        ix = FakeIx(
            vms={"baml-runner-1", "baml-runner-2"},
            revs={"baml-runner-1": REV, "baml-runner-2": REV},
            online={1, 2},
            markers={"baml-runner-1"},
        )
        self.assertEqual(await self.reconcile_with(ix), 0)
        self.assertIn(
            ("baml-runner-1", ("rm", "-f", "/var/lib/ix-runner/repaired")),
            ix.calls,
        )

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

    async def test_creates_run_concurrently_under_the_cap(self):
        # The execute phase overlaps guest boots but never exceeds the
        # semaphore; with 8 creates and concurrency 3, peak in-flight is 3.
        ix = FakeIx(vms=set(), revs={}, online=set(), markers=set())
        ix.create_delay = 0.01
        env = dict(ENV, POOL_SIZE="8", MAX_REPLACEMENTS="8", CONCURRENCY="3")
        self.assertEqual(await self.reconcile_with(ix, env=env), 8)
        self.assertGreater(ix.max_in_flight, 1)
        self.assertLessEqual(ix.max_in_flight, 3)

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

    async def test_one_failed_create_does_not_abort_the_run(self):
        # A bad template rev spends the budget and exits non-zero, but every
        # member in budget is still attempted (no half-scanned pool).
        ix = FakeIx(vms=set(), revs={}, online=set(), markers=set(), broken=True)
        with self.assertRaises(SystemExit):
            await self.reconcile_with(ix)
        creates = [c for _, c in ix.calls if c[0] == "create"]
        self.assertEqual(len(creates), 2)


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
        self.assertIn("secondary rate limit", str(caught.exception))

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


if __name__ == "__main__":
    unittest.main()
