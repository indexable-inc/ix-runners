# Design notes

Why v2 behaves the way it does. The [README](../README.md) says what it
does; this says why. v1 - a fixed pool of persistent machines whose power
state followed a level - lives in git history, and its design notes are
worth reading for the failures that shaped both versions.

## The model in one paragraph

Every runner is a machine that exists for exactly one job. When a job on
the repository's default branch succeeds, the machine that ran it is
snapshotted before it dies, and that snapshot becomes the **seed** for its
**lineage** - the set of `runs-on` labels the job asked for. Every later
runner of that lineage is restored from the seed: it boots in seconds with
the previous green build's entire disk - toolchains, `target/`,
`node_modules`, every cache - already in place, runs its one job, and is
deleted. Warmth is a property of the lineage, not of any machine, so there
is no cold-runner case, no idle pool to manage, and no state a job can
leak into the machine that runs the next one.

## Why per-job machines, when v1 said "do not make these runners ephemeral"

v1's warmth lived in long-lived per-slot HOMEs, so ephemerality would have
thrown the caches away - hence the warning. v2 moves the warmth into the
seed snapshot, where it survives every machine. That inversion deletes,
outright:

- the idle question (nothing idles: a runner is busy or it is deleted),
- the repair/two-strike machinery (nothing is repaired; anything unhealthy
  is deleted and the next restore is seconds away),
- the registration-token secret store and its rotation subtleties (each
  machine gets its own single-job JIT credential, written to it alone),
- co-tenant slot isolation (per-slot users, cgroup fairness, Yama): one
  job per machine means the machine boundary IS the isolation,
- cross-job contamination: a PR job's writes die with its fork and can
  never reach another job or the seed.

## Lineages and seeds, precisely

A **lineage** is an exact `runs-on` label set. Jobs opt in by carrying the
pool's marker label (`runner-label`, default `ix`); everything else in the
queue is invisible to this pool. The lineage key is the first 8 hex chars
of SHA-256 over the sorted label set.

The **seed holder** is one stopped machine per lineage, named
`<pool>-seed-<lineage>-<rev8>`, kept only because a snapshot cannot outlive
its source machine (probed 2026-08-19: deleting the source makes both
`snapshots.list` and `snapshots.restore` answer NotFound). A stopped
machine bills storage alone. Each holder carries exactly one snapshot -
the one taken at its promotion - so snapshot GC is machine GC.

**Promotion**: when a completed default-branch job succeeded and its
runner machine is still alive, the reconcile snapshots that machine, waits
for the snapshot to be ready, renames the old holder aside, renames the
winner into the holder name, and stops it. The old holder is deleted last.
A crash anywhere leaves either no change or two holders, and the next tick
keeps the one with the newer ready snapshot. Only default-branch successes
promote: PR state never enters the seed lineage, which is both the cache
poisoning story and the reason a seed is always a state the trunk actually
reached.

**Rev roll**: `<rev8>` in the holder name is the runner-config rev the
lineage descends from. When the config rev changes, existing holders stop
matching, read as absent, and are GC'd; the next job of each lineage boots
cold from the new template and re-establishes the seed. Encoding the rev
in the name keeps the check stateless and probe-free (the holder is
stopped; there is no guest to ask).

## The tick

Level-based and stateless, inherited from v1 verbatim because every clause
was paid for: each tick observes machines, registrations and the GitHub
queue fresh, decides from that snapshot alone, and moves toward the level.
A missed tick costs latency, never correctness. The two wave rules hold:
an event tick (`workflow_run: requested` fires before the wave's jobs are
queued) may only add capacity, and a tick that could not read GitHub makes
no scale-down decision at all.

Per lineage with queued demand:

    want = queued_jobs > 0 ? queued_jobs + headroom : min_warm
           (admitted within the remaining global max-runners budget;
            headroom pads live demand, min-warm floors quiet lineages -
            they deliberately do not stack)
    have = registered idle runners of that lineage
    spawn(want - have): restore from seed, or cold-boot the template
                        when no matching holder exists

Spawning is: create the machine, mint a JIT runner config named after the
machine with the lineage's exact labels, write it to
`/var/lib/ix-runner/jitconfig`, where a systemd path unit is watching. JIT
runners take one job and deregister themselves; GitHub's own busy-refusal
(422 on DELETE) remains the only lock anywhere in the system, used when
retiring an idle standby that a job might be landing on.

A runner machine whose registration is gone is finished: it is either
promoted (above) or deleted. A runner whose registrations are all still
OFFLINE past the idle grace never came up (or died mid-life): it is
deregistered and deleted - an offline runner cannot hold a job, and one
racing online turns the deregister into the 422 refusal. A holder found
RUNNING is stopped (a promotion died between rename and stop). Retirement
of *idle* standbys past `idle-grace-seconds` happens only on scheduled
ticks, derived from GitHub's own job timestamps, never counted.

## What stayed from v1, and why

- **Hosted-runner-only control plane**, enforced at startup: this process
  holds IX_TOKEN and a repo-admin credential and manages the very machines
  a self-hosted runner would be.
- **Credential vending over a PAT**: the reconcile trades its OIDC
  identity for a repo-scoped installation token (`token-source: ix`); the
  PAT path survives as the fallback era. The App key never enters any
  repo. Vending is an in-protocol RPC in the SDK core, not a REST
  endpoint (verified by string-dumping the Python ix_sdk 0.7.2 native
  module: the op sits in the RPC method table, and the binary carries no
  REST path at all), and the published TypeScript SDK (0.7.1, the version
  this package pins) does not carry the op yet - so `token-source: ix`
  refuses with a message naming that blocker, and the PAT path is the
  working era until `@indexable/sdk` ships `ci()`.
- **Host pinning and redirect refusal** on every credentialed request, the
  strict servable/label matching rule, complete-or-refuse pagination of
  the runner listing, `::add-mask::` before any credential can print, and
  control-character stripping on every remote string.
- **Budgeted convergence**: creations per tick are capped and spent at
  admission, so a bad template rev stalls loudly instead of thrashing.

## What this costs, honestly

- **Pickup latency on a cold lineage tick**: an on-demand spawn is an
  event-tick delay plus a ~5s restore plus runner registration. `min-warm`
  standbys exist for lineages where that matters; they are running
  machines and bill as such.
- **One stopped anchor machine per lineage**: storage-priced rent paid to
  the snapshot-lifetime rule. If snapshots become account-level objects,
  the holders delete and `restore` is the only primitive left.
- **The seed is as fresh as the last green default-branch run**: a fork
  rebuilds the delta since then. That is cargo/npm doing reconciliation,
  not a correctness risk.
