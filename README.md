# ix-runners

> **Generated repository. Do not edit here.** This repository is a projection of `satellites/ix-runners` in the private ix monorepo; a bot overwrites it on every ix main push, so direct changes are lost. To propose a change, open an issue; pull requests here are closed automatically.

Ephemeral fork-per-job GitHub Actions runners on [ix](https://ix.dev) VMs.

## Maintenance mode

New feature work happens in the ix-hosted webhook control plane (in the ix
monorepo), not here. (This repository's own design notes call this
implementation v2; the webhook control plane is its successor.)

A pool is owned by exactly one control plane, never both: the reconcile has
no internal lock, so two planes managing one pool double-spawn and
double-promote (see the warning in `action.yml`). Migrating a pool means
removing it here in the same change that adds it there.

This repository still matters for two things:

- Pools not yet migrated. Until the webhook control plane grows its own
  reconcile sweep, only pools here have missed-webhook protection.
- GHES and organizations that restrict GitHub App installation.

Archiving is gated on that sweep landing and the last pool migrating.
Bug fixes remain welcome until then.

Every runner is a machine that exists for exactly one job. When a job on
your default branch goes green, the machine that ran it is snapshotted and
becomes the **seed** for its label set: every later job with those labels
boots a fork of it in seconds - toolchains, `target/`, `node_modules`,
every cache already warm - runs, and is deleted. Warm caches without shared
machines: a PR job's writes die with its fork and can never reach another
job or the seed.

Why it looks this way: [docs/design.md](./docs/design.md).

## The machine lifecycle, in one picture

```mermaid
flowchart LR
    Q[job queued<br/>runs-on: self-hosted, ix] -->|reconcile tick| S{seed for this<br/>label set?}
    S -->|yes| F[fork the seed<br/>boots in seconds, caches warm]
    S -->|no, first time| C[cold boot from<br/>the pool's OCI image]
    F --> R[machine runs its ONE job<br/>on a single-job JIT credential]
    C --> R
    R -->|green, on the default branch| P[snapshot the machine:<br/>it becomes the new seed]
    R -->|anything else<br/>PR, red, cancelled| D[machine deleted<br/>its writes die with it]
    P --> D2[old seed deleted]
    P -.->|next job of this label set forks it| F
```

Two things fall out of the shape. Warmth is a property of the **label
set**, not of any machine - so there is no idle pool, no cold-start tax
after the first green run, and nothing to repair. And isolation is the
machine boundary - a PR job runs on a fork that is deleted afterward, so
nothing it writes can ever reach another job or the seed.

## Setup

1. Add two Actions secrets: `IX_TOKEN` (the ix account the VMs bill to) and
   `RUNNER_PAT` (fine-grained PAT, Administration read/write on the repo).

   The built-in `GITHUB_TOKEN` cannot stand in for the PAT: workflow
   permissions have no `administration` scope, so it structurally cannot
   mint runner credentials.

2. Write `.github/ix-runners.toml` and name the runner image, the one key
   without a default:

   ```toml
   image = "ix/runner:2026-10-08"   # a version tag of the default runner image
   ```

   `ix/runner` (git, curl, ca-certificates and the GitHub Actions runner,
   built for linux/amd64 and linux/arm64) is published by the ix repository's
   weekly image job. Tags are versions and never move; `latest` is refused.
   For a different toolchain, build your own image from it and name that
   reference here (a private registry takes a stored secret).

3. Optionally set the dials in the same file. Every other key has a working
   default:

   ```toml
   region = "us-west-1"
   max-runners = 16        # global cap on concurrently existing machines
   headroom = 1            # idle standbys beyond queued demand, per lineage
   min-warm = 0            # standbys per known lineage even with no demand
   idle-grace-seconds = 900
   seed-rebuild-interval-seconds = 604800  # weekly cold seed refresh
   ```

4. Add the workflow below, merge, and put `runs-on: [self-hosted, ix]` in
   the workflows you want on the fleet. The `ix` marker label is what opts
   a job in; every distinct label set you use becomes its own seed lineage.

### The workflow

```yaml
name: ix runners

on:
  pull_request:
    types: [closed]
  push:
    # Deleted-branch pushes carry `deleted: true`; the action uses that event
    # to close the branch's outstanding jobs and remove their runner VMs.
    branches: [main, '**']
  schedule:
    # The steady tick: promotion, retirement, cleanup. Best effort - GitHub
    # drops scheduled runs under load, and a missed tick costs latency,
    # never correctness.
    - cron: "*/15 * * * *"
  workflow_dispatch:
  workflow_run:
    # The fast path: fires when any run is requested, so capacity is being
    # created while the wave's jobs are still queueing. "**" includes this
    # workflow itself (workflow_run cannot exclude by name); the follow-up
    # it requests coalesces into the concurrency group below and GitHub
    # caps the chain, so the noise is one extra no-op tick, not a loop.
    workflows: ["**"]
    types: [requested]

permissions:
  contents: read
  actions: read

# One reconcile at a time, never cancelled mid-create: a cancelled run can
# leave a machine created but not yet registered.
concurrency:
  group: ix-runners
  cancel-in-progress: false

jobs:
  reconcile:
    # GITHUB-HOSTED only. A runner VM must never see IX_TOKEN or RUNNER_PAT.
    runs-on: ubuntu-latest
    steps:
      # Pinned by commit, not by tag: this job holds IX_TOKEN and a
      # repo-admin PAT, and checkout runs before the reconcile does - it
      # can rewrite the environment the reconcile then reads, through
      # $GITHUB_ENV.
      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7
        with:
          persist-credentials: false

      - uses: indexable-inc/ix-runners@<rev>
        with:
          ix-token: ${{ secrets.IX_TOKEN }}
          runner-pat: ${{ secrets.RUNNER_PAT }}
```

### Pool mode: pools shipped in this repository

A pool ix maintains for you lives under [`pools/`](./pools) in this repo -
its spec (`ix-runners.toml`, which names the runner image) - and your
repository carries only the workflow. Pass `pool: <name>` instead of
`config-file`:

```yaml
      - uses: indexable-inc/ix-runners@<sha>   # pin by full commit sha
        with:
          ix-token: ${{ secrets.IX_TOKEN }}
          runner-pat: ${{ secrets.RUNNER_PAT }}
          pool: baml
```

In pool mode the spec comes from the action's own commit, and seeds key on
the image reference in it - so bumping the `uses:` sha to one that names a
new image tag is what re-seeds the fleet, and a merge in your repository
never can. The reconcile reads nothing from your working tree: drop the
checkout step from the workflow above, and the `push` trigger stops mattering - `schedule`, `workflow_dispatch` and
`workflow_run` are enough.

## How it works

Warmth is copy-on-write. A lineage's *seed* is an immutable ix snapshot
(disk and memory) of the machine that ran its last green default-branch
job. Every runner is a fork of that snapshot: it boots in about a second
with everything the green run left behind - the toolchains, `$HOME`
caches, compiled artifacts - already on disk, and its writes land in its
own private copy-on-write layer. Nothing a job writes can reach the seed
or any sibling fork; a fork's writes die with the fork. The seed only
ever advances by *promotion*: a fresh snapshot of a machine that just
ran a green default-branch job. That is the whole trick - `ubuntu-latest`
spends minutes re-downloading what your last run already built, a fork
starts where the last green run stopped.

Each tick is level-based and stateless: it observes the machines, the
runner registrations and the job queue fresh, decides from that snapshot
alone, and converges. Every machine's role rides its NAME
(`<pool>-run-<lineage>-<nonce>`, `<pool>-seed-<lineage>-<image-hash>`), so there
is no state store to disagree with reality.

- Demanded job: a machine is spawned for it (plus `headroom`) - forked
  from its lineage's seed, or booted cold from the pool's image when the lineage
  has none yet. Each machine gets its own single-job JIT credential,
  minted for it by name and written to it alone.
- Green default-branch job: the machine that ran it is snapshotted and
  swapped in as its lineage's seed before being stopped. Only
  default-branch successes promote - PR state never enters a seed.
- Finished runner (its one-job registration is gone): deleted.
- A new `image` reference in the spec: every seed of the old image reads
  as absent and is deleted; each lineage re-seeds from its next green run
  on the new image.
- Idle standby past `idle-grace-seconds`: deregistered and deleted -
  GitHub refuses (422) to deregister a runner that is mid-job, and that
  refusal is the one lock in the system.
- A tick that could not read the queue makes no scale-down decision at
  all, and an event tick only ever adds capacity.

Failures are per step: one machine's failure is logged as an Actions
error and the run continues; the job summary carries a table of what
happened.

## Security model

- `IX_TOKEN` and `RUNNER_PAT` live in Actions secrets and never reach a
  runner VM. The reconcile refuses to start unless `RUNNER_ENVIRONMENT`
  says it is on a GitHub-hosted runner: it is the control plane, so
  running it on the fleet would hand both secrets to the machines they
  exist to control. On GHES or ARC, set `IX_RUNNERS_ALLOW_NON_HOSTED=1`
  to accept that explicitly - which also lets `GITHUB_API_URL` name your
  own https API base. Everywhere else the API base is pinned to
  `api.github.com`, because `GITHUB_API_URL` is an environment variable
  any earlier step in the job can rewrite.
- No credentialed request follows a redirect: a 30x would re-aim the
  Authorization header at whatever host `Location` names.
- The only credential a runner VM ever holds is its own single-job JIT
  config, which can take exactly one job as exactly the runner it names,
  and is consumed (moved out of the watched path) before the job starts -
  so a spent credential can never ride into a seed snapshot. It is masked
  in Actions logs the moment it is minted.
- An expired or revoked `RUNNER_PAT` presents as HTTP 401; the reconcile
  stops and says exactly that.
- Jobs run with the machine as the isolation boundary: no co-tenants, no
  shared caches, nothing to escape into. A PR job can poison at most its
  own fork, which is deleted. Still: seeds descend only from default-branch
  runs, so gate who can push there as you already do.
- Everything that runs your CI is in this repository, readable.

## What differs from ubuntu-latest

The runner VM boots the OCI image the spec names (`ix/runner`: Debian-based,
  the GitHub Actions runner preinstalled):

- Tooling is whatever the image carries: anything a job expects "to just be
  there" (Go, docker, protoc) must be in your own image built from
  `ix/runner`, or installed by the job.
- `$HOME` is the warmth: whatever a green default-branch
  run leaves there is what the next fork of that lineage boots with
  (copy-on-write, so ten concurrent forks share the seed's bytes and
  none can dirty another).
- `token-source: ix` deletes the PAT entirely: the reconcile trades its
  OIDC identity (`permissions: id-token: write`) for a repo-scoped App
  installation token minted by ix. The repository comes from the OIDC
  token's signed claims, so the credential cannot be minted for a repo
  the run has not proved it is running for.

## Roadmap

- The ix-hosted control plane (GitHub App webhooks instead of a workflow in
  your repo) is now being built in the ix monorepo; see "Maintenance mode"
  above for what stays here and the pool-ownership rule.
