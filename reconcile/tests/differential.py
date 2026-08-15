"""Differential proof: run the OLD reconcile and the NEW one over the same
scenarios with the same fakes, and diff every observable.

This is the behaviour-identity gate for the module split. The unit suite
proves the new code is CORRECT; this proves it is the SAME, which is a
different claim and the one a pure refactor has to make.

NOT part of `unittest discover` - it needs a copy of the old implementation
to compare against, which only exists while a refactor is in flight:

    git show <pre-refactor-rev>:reconcile/ix_runners.py > /tmp/old.py
    OLD_RECONCILE=/tmp/old.py python3 reconcile/tests/differential.py

Keeping it in the tree means the next person restructuring this can re-run
the same argument instead of inventing one.
"""

import asyncio
import contextlib
import importlib.util
import io
import os
import pathlib
import sys
from unittest import mock

# Point this at the pre-refactor ix_runners.py to re-prove identity against
# any earlier revision:  git show <rev>:reconcile/ix_runners.py > /tmp/old.py
OLD_PATH = os.environ.get("OLD_RECONCILE", "/tmp/orig_full.py")
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent.parent))

import reconcile.tests.test_ix_runners as T  # the fakes live here
from reconcile.ix_runners import reconcile as new_reconcile


def load_old():
    spec = importlib.util.spec_from_file_location("old_ix_runners", OLD_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["old_ix_runners"] = mod
    spec.loader.exec_module(mod)
    return mod


OLD = load_old()


def observable(ix, out, ret, exc):
    """Everything a caller or an operator could notice."""
    text = out.getvalue()
    # Drop the ::add-mask:: of the PAT (identical) and normalise nothing else.
    decision = [l for l in text.splitlines() if l.startswith("DECISION ")]
    done = [l for l in text.splitlines() if l.startswith("reconcile done:")]
    return {
        "created": sorted(ix.created),
        "started": sorted(ix.started),
        "stopped": sorted(ix.stopped_by_run),
        "deleted": sorted(n for n, c in ix.calls if c == ("delete",)),
        "secret_sets": [c for _, c in ix.calls if c[0] == "secret-set"],
        "secret_deletes": [c for _, c in ix.calls if c[0] == "secret-delete"],
        "deregisters": sorted(
            c[1] for _, c in ix.calls if c[0] == "DELETE" and "/actions/runners/" in c[1]
        ),
        "probed": sorted({n for n, c in ix.calls if c[0] == "shell"}),
        "demand_scans": len([c for _, c in ix.calls if c[0] == "GET" and "/actions/runs" in c[1]]),
        "guest_cmds": sorted(
            str(c) for n, c in ix.calls if c and c[0] in ("systemctl", "touch", "sh")
        ),
        "decision": decision,
        "done": done,
        "returned": ret,
        "raised": repr(exc) if exc else None,
    }


async def run_one(which, ix, env):
    out = io.StringIO()
    ret = exc = None
    if which == "old":
        patches = [
            mock.patch.dict("os.environ", env, clear=True),
            mock.patch.object(OLD, "desired_rev", return_value=T.REV),
            mock.patch.object(OLD, "github_api", ix.github_api),
            mock.patch.object(OLD, "create_options", lambda **kw: kw),
            mock.patch.object(OLD, "DEREGISTER_PAUSE", 0.0),
        ]
        fn = OLD.reconcile
    else:
        patches = [
            mock.patch.dict("os.environ", env, clear=True),
            mock.patch("reconcile.ix_runners.desired_rev", return_value=T.REV),
            mock.patch("reconcile.github.github_api", ix.github_api),
            mock.patch("reconcile.machines.create_options", lambda **kw: kw),
            mock.patch("reconcile.github.DEREGISTER_PAUSE", 0.0),
        ]
        fn = new_reconcile
    with contextlib.ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        with contextlib.redirect_stdout(out):
            try:
                ret = await fn(ix)
            except BaseException as error:  # SystemExit included
                exc = error
    return observable(ix, out, ret, exc)


def scenarios():
    J, F, P = T.job, T.finished, T.autoscale_pool
    A = T.AUTO_ENV
    E = T.ENV
    yield "idle pool, autoscaling off", lambda: P(running=[1, 2], online=[1, 2]), E | {"POOL_SIZE": "2"}
    yield "stopped members parked", lambda: P(running=[1], online=[1], stopped=[2, 3, 4, 5, 6]), A
    yield "surplus scale-down", lambda: P(running=[1, 2, 3, 4], online=[1, 2, 3, 4], jobs=[F()]), A
    yield "scale-up from demand", lambda: P(running=[1], online=[1], stopped=[2, 3, 4, 5, 6], jobs=[J()] * 3), A
    yield "foreign labels only", lambda: P(running=[1], online=[1], stopped=[2, 3, 4, 5, 6], jobs=[J(labels=T.FOREIGN)] * 5 + [F()]), A
    yield "event tick, surplus", lambda: P(running=[1, 2, 3, 4], online=[1, 2, 3, 4], jobs=[F()]), A | {"GITHUB_EVENT_NAME": "workflow_run"}
    yield "busy member, surplus", lambda: P(running=[1, 2, 3], online=[1, 2, 3], busy={3}, jobs=[J("in_progress"), F(runner="baml-r2-1")]), A | {"MIN_WARM": "2"}
    yield "deregister 422 on stop", lambda: P(running=[1, 2], online=[1, 2], busy_at_delete={2}, jobs=[F(runner="baml-r2-1")]), A
    yield "failed machine", lambda: P(running=[1], online=[1], failed=[2], info={"baml-runner-2": {"created_at": T.now_ms() - 30_000}}), A | {"POOL_SIZE": "2"}
    yield "warming machine", lambda: P(running=[1, 2], online=[1], stopped=[3, 4, 5, 6], jobs=[J()] * 2, info={"baml-runner-2": {"started_at": T.now_ms() - 20_000}}), A
    yield "unreadable queue", lambda: P(running=[1, 2, 3], online=[1, 2, 3], stopped=[4, 5, 6], demand_error=T.http_error(502, "Bad gateway")), A
    yield "missing label refusal", lambda: P(running=[1, 2, 3, 4, 5, 6], online=[1, 2, 3, 4, 5, 6]), {k: v for k, v in (A | {"MIN_WARM": "2", "MAX_ONLINE": "4"}).items() if k != "RUNNER_LABEL"}
    yield "impossible range", lambda: P(running=[1, 2], online=[1, 2]), A | {"MIN_WARM": "4", "MAX_ONLINE": "2"}
    yield "empty pool bootstrap", lambda: T.FakeIx(vms=set(), revs={}, online=set(), markers=set()), E
    yield "stale rev roll", lambda: P(running=[1, 2], online=[1, 2], jobs=[F()]), A
    yield "hand-stopped member", lambda: P(running=[1], online=[1], stopped=[2]), E | {"POOL_SIZE": "2"}
    yield "unknown status", lambda: P(running=[1], online=[1]), A | {"POOL_SIZE": "2"}
    yield "repair path", lambda: P(running=[1, 2], online=[1], registered={1, 2}, jobs=[F()]), A | {"MIN_WARM": "2"}
    def struck():
        ix = P(running=[1, 2], online=[1], registered={1, 2}, jobs=[F()])
        ix.markers = {"baml-runner-2"}
        return ix
    yield "two-strike replace", struck, A | {"MIN_WARM": "2"}
    yield "prune above pool size", lambda: P(running=[1, 2, 3], online=[1, 2, 3]), A | {"POOL_SIZE": "2", "MIN_WARM": "2", "MAX_ONLINE": "2"}
    yield "max stops cap", lambda: P(running=[1, 2, 3, 4, 5, 6], online=[1, 2, 3, 4, 5, 6], jobs=[F()]), A | {"MAX_STOPS": "2"}
    yield "slots divide demand", lambda: P(running=[1], online=[1], stopped=[2, 3, 4, 5, 6], slots=4, jobs=[J()] * 5), A
    yield "idle inside the grace", lambda: P(running=[1, 2], online=[1, 2], jobs=[F(runner="baml-r2-1", ago=60), F(runner="baml-r1-1", ago=90)]), A
    yield "idle window shorter than grace", lambda: P(running=[1, 2], online=[1, 2], jobs=[F(runner="baml-r1-1", ago=30)]), A
    yield "no idle history at all", lambda: P(running=[1, 2], online=[1, 2]), A
    yield "mixed grace boundary", lambda: P(running=[1, 2, 3], online=[1, 2, 3], jobs=[F(runner="baml-r3-1", ago=60), F(runner="baml-r2-1", ago=5000)]), A


async def main():
    bad = 0
    for name, build, env in scenarios():
        old_ix, new_ix = build(), build()
        # Scenario builders that vary a marker set must not share mutable state.
        old = await run_one("old", old_ix, dict(env))
        new = await run_one("new", new_ix, dict(env))
        if old == new:
            print(f"  IDENTICAL  {name}")
            continue
        bad += 1
        print(f"  DIFFERS    {name}")
        for key in old:
            if old[key] != new[key]:
                print(f"      {key}:\n        old={old[key]!r}\n        new={new[key]!r}")
    print(f"\n{bad} differing scenario(s) of {len(list(scenarios()))}")
    return bad


if __name__ == "__main__":
    sys.exit(1 if asyncio.run(main()) else 0)
