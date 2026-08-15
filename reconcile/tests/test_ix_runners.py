from __future__ import annotations

import asyncio
import subprocess
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

from reconcile.ix_runners import desired_rev, list_runners, member_online, reconcile

ROOT = Path(__file__).resolve().parents[2]

REV = "a" * 40
OLD_REV = "b" * 40

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
        broken=False,
        page_size=100,
    ):
        self.vms = vms  # existing VM names
        self.revs = revs  # name -> baked config rev (missing = unreachable)
        self.online = online  # pool member numbers with an online runner
        self.markers = markers  # names carrying the two-strike marker
        self.busy = busy  # pool member numbers with a runner mid-job
        # members that pick up a job AFTER the scan snapshot: idle in the
        # runners listing, but GitHub 422s the registration delete.
        self.busy_at_delete = busy_at_delete
        self.broken = broken  # every create fails (bad template rev)
        self.page_size = page_size  # runner listing page size
        self.calls = []
        self.create_delay = 0.0
        self.in_flight = 0
        self.max_in_flight = 0

    def machines(self):
        return FakeMachines(self)

    def secrets(self):
        return FakeSecrets(self)

    def github_api(self, pat, repo, path, *, method="GET"):
        self.calls.append((None, (method, path)))
        if path.startswith("/actions/runners?"):
            runners = [
                {
                    "id": 1000 + member,
                    "name": f"baml-r{member}-1",
                    "status": "online",
                    "busy": member in self.busy,
                }
                for member in sorted(self.online | self.busy)
            ]
            start = (int(path.rsplit("page=", 1)[1]) - 1) * self.page_size
            return {
                "total_count": len(runners),
                "runners": runners[start : start + self.page_size],
            }
        if path == "/actions/runners/registration-token":
            return {"token": "REGTOKEN"}
        if method == "DELETE" and path.startswith("/actions/runners/"):
            runner_id = int(path.rsplit("/", 1)[1])
            member = runner_id - 1000
            if member in self.busy_at_delete:
                raise urllib.error.HTTPError(path, 422, "busy", None, None)
            return {}
        raise AssertionError(f"unexpected API path {path}")


class ReconcileTest(unittest.IsolatedAsyncioTestCase):
    async def reconcile_with(self, ix, env=ENV):
        with (
            mock.patch.dict("os.environ", env),
            mock.patch("reconcile.ix_runners.desired_rev", return_value=REV),
            mock.patch("reconcile.ix_runners.github_api", ix.github_api),
            mock.patch("reconcile.ix_runners.create_options", lambda **kw: kw),
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
        dereg = ix.calls.index((None, ("DELETE", "/actions/runners/1001")))
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


class MemberOnlineTest(unittest.TestCase):
    def test_prefix_does_not_cross_member_boundaries(self):
        runners = [{"name": "baml-r10-1", "status": "online"}]
        self.assertFalse(member_online(runners, "baml", 1))
        self.assertTrue(member_online(runners, "baml", 10))

    def test_offline_runner_does_not_count(self):
        runners = [{"name": "baml-r1-1", "status": "offline"}]
        self.assertFalse(member_online(runners, "baml", 1))


if __name__ == "__main__":
    unittest.main()
