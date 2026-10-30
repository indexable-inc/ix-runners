# Design notes

Why ix-runners behaves the way it does. The [README](../README.md) says what it does. v1 was a fixed pool of persistent VMs whose power state followed a level; its notes are in git history.

## Why one VM per job

v1 kept warmth in long-lived per-slot `$HOME`s, so ephemeral runners would have thrown the caches away. v2 keeps warmth in the seed snapshot, which outlives every VM. That deleted the idle question, the repair and two-strike logic, the registration-token store, per-slot user and cgroup isolation, and cross-job contamination.

## Lineages and seeds

A lineage is an exact `runs-on` label set that includes the pool's marker label (`runner-label`, default `ix`). Its key is the first 8 hex chars of SHA-256 over the sorted labels.

The seed holder is one stopped VM per lineage, `<pool>-seed-<lineage>-<rev8>`. It exists because a snapshot cannot outlive its source VM: deleting the source made `snapshots.list` and `snapshots.restore` answer NotFound (probed 2026-08-19). A stopped VM bills storage only. Each holder carries exactly one snapshot, so snapshot GC is VM GC.

Promotion snapshots the green VM, waits for the snapshot to be ready, renames the old holder aside, renames the winner into the holder name and stops it, then deletes the old holder. A crash anywhere leaves no change or two holders, and the next tick keeps the one with the newer ready snapshot. A holder found running is stopped, since a promotion died between rename and stop.

`<rev8>` hashes the pool's `image` reference. A new image makes old holders stop matching, so they read as absent and are deleted. The hash lives in the name because the holder is stopped and has no guest to ask.

## The tick

Each tick observes VMs, registrations and the GitHub queue fresh and moves toward the level. A missed tick costs latency, never correctness. An event tick (`workflow_run: requested` fires before the wave's jobs queue) may only add capacity. A tick that could not read GitHub makes no scale-down decision.

    want = queued_jobs > 0 ? queued_jobs + headroom : min_warm
    have = registered idle runners of the lineage
    spawn(want - have), within the remaining max-runners budget

`headroom` pads live demand and `min-warm` floors quiet lineages. They do not stack. Creations per tick are capped and spent at admission, so a bad image tag stalls loudly instead of thrashing.

A spawn creates the VM, mints a JIT runner config named after it with the lineage's exact labels, and writes it to `/var/lib/ix-runner/jitconfig`, where a systemd path unit watches. Labels ride each JIT credential, so a runner cannot serve the wrong lineage.

A runner whose registrations all stay offline past the idle grace never came up or died. It is deregistered and deleted. An offline runner cannot hold a job, and one racing online turns the deregister into GitHub's 422 refusal. Idle standbys retire only on scheduled ticks, timed from GitHub's own job timestamps.

## Credentials

- The control plane runs only on hosted runners, enforced at startup, because it holds `IX_TOKEN` and a repo-admin credential for the VMs a self-hosted runner would be.
- `token-source: ix` trades OIDC for an installation token. Vending is an RPC op in the SDK core with no REST path. `@indexable/sdk` 0.7.2 exposes it as `client.ci().githubRunnerToken(oidcJwt)`. `vend.ts` fetches the JWT with the same host pinning and redirect refusal as `github.ts`.
- Runner listing pagination is complete or refused. `::add-mask::` runs before any credential can print, and every remote string has control characters stripped.

## Newer runner images

From the 2026-10-16 entrypoint on, `ix/runner` images no longer power off after the job. They scrub the guest, write `/run/ix-runner/capture-ready` and wait for the next JIT config, and the webhook control plane commits a seed only after reading that marker. This Action does not read the marker, so it could snapshot a runner before its scrub. Keep pools here on `ix/runner:2026-10-08`, or move them to the webhook control plane.

## Costs

- An on-demand spawn waits for an event tick, a restore of about 5 s and runner registration. `min-warm` standbys hide that and bill as running VMs.
- One stopped holder per lineage pays storage rent for the snapshot-lifetime rule. If snapshots become account-level objects, the holders go away.
- A seed is as fresh as the last green default-branch run, so a fork rebuilds the delta since then.
