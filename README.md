# ix-runners

> **Generated repository. Do not edit here.** This repository is a projection of `satellites/ix-runners` in the private ix monorepo; a bot overwrites it on every ix main push, so direct changes are lost. To propose a change, open an issue; pull requests here are closed automatically.

Ephemeral fork-per-job GitHub Actions runners on [ix](https://ix.dev) VMs.

Every runner is a VM that exists for one job. When a job on your default branch goes green, its VM is snapshotted and becomes the **seed** for that job's label set. Every later job with those labels boots a fork of the seed in about a second, with toolchains, `target/`, `node_modules` and `$HOME` caches already warm. It runs its one job and is deleted. A PR job's writes die with its fork and never reach another job or the seed. Why it works this way is in [docs/design.md](./docs/design.md).

## Maintenance mode

New work happens in the ix-hosted webhook control plane, which installs as a GitHub App and needs no workflow in your repository. This repository stays for pools not yet migrated, and for GHES or organizations that restrict GitHub App installation. Until the webhook control plane has its own reconcile sweep, only pools here get missed-webhook protection. Bug fixes are welcome until the last pool migrates.

A pool belongs to exactly one control plane. The reconcile has no internal lock, so two planes on one pool double-spawn and double-promote. Remove a pool here in the same change that adds it there.

## Setup

1. Add two Actions secrets. `IX_TOKEN` is the ix account the VMs bill to. `RUNNER_PAT` is a fine-grained PAT with Administration read/write on the repo. `GITHUB_TOKEN` cannot replace the PAT, because workflow permissions have no `administration` scope to mint runner credentials.
2. Write `.github/ix-runners.toml` with the one required key:

   ```toml
   image = "ix/runner:2026-10-08"
   ```

   `ix/runner` is Debian with git, curl, ca-certificates and the Actions runner, for linux/amd64 and linux/arm64. Tags are versions and never move, and `latest` is refused. Anything else a job expects, such as Go, docker or protoc, goes in your own image built from `ix/runner` or gets installed by the job. A private registry takes a stored secret.
3. Optional keys, shown with their defaults:

   ```toml
   region = "us-west-1"
   max-runners = 16        # global cap on concurrently existing VMs
   headroom = 1            # idle standbys beyond queued demand, per lineage
   min-warm = 0            # standbys per known lineage even with no demand
   idle-grace-seconds = 900
   seed-rebuild-interval-seconds = 604800  # weekly cold seed refresh
   ```

4. Add the workflow below and put `runs-on: ix` on the jobs you want on ix. The `ix` label opts a job in, and every distinct label set becomes its own seed lineage.

```yaml
name: ix runners

on:
  pull_request:
    types: [closed]
  push:
    # A deleted-branch push closes the branch's outstanding jobs and removes their VMs.
    branches: [main, '**']
  schedule:
    # Promotion, retirement and cleanup. GitHub drops scheduled runs under load;
    # a missed tick costs latency, never correctness.
    - cron: "*/15 * * * *"
  workflow_dispatch:
  workflow_run:
    # Fires when any run is requested, so capacity starts while jobs queue.
    # "**" includes this workflow; its follow-up coalesces into the concurrency group.
    workflows: ["**"]
    types: [requested]

permissions:
  contents: read
  actions: read

# One reconcile at a time and never cancelled: a cancelled run can leave a VM
# created but not registered.
concurrency:
  group: ix-runners
  cancel-in-progress: false

jobs:
  reconcile:
    # GitHub-hosted only. A runner VM must never see IX_TOKEN or RUNNER_PAT.
    runs-on: ubuntu-latest
    steps:
      # Pinned by commit: this job holds IX_TOKEN and a repo-admin PAT, and
      # checkout can rewrite the reconcile's environment through $GITHUB_ENV.
      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7
        with:
          persist-credentials: false

      - uses: indexable-inc/ix-runners@<rev>
        with:
          ix-token: ${{ secrets.IX_TOKEN }}
          runner-pat: ${{ secrets.RUNNER_PAT }}
```

### Pool mode

A pool ix maintains for you lives under [`pools/`](./pools) as its own `ix-runners.toml`. Pass `pool: <name>` instead of `config-file`, and pin the action by full commit sha:

```yaml
      - uses: indexable-inc/ix-runners@<sha>
        with:
          ix-token: ${{ secrets.IX_TOKEN }}
          runner-pat: ${{ secrets.RUNNER_PAT }}
          pool: baml
```

The spec then comes from the action's own commit, and seeds key on its image reference. Bumping the `uses:` sha to one with a new image tag re-seeds the fleet, and nothing merged in your repository can. The reconcile reads nothing from your working tree, so drop the checkout step. The `push` trigger stops mattering.

## How it works

Each tick is level-based and stateless. It reads VMs, runner registrations and the job queue fresh and converges from that snapshot. A VM's role rides its name (`<pool>-run-<lineage>-<nonce>`, `<pool>-seed-<lineage>-<image-hash>`), so there is no state store to disagree with reality.

- A queued job gets a VM, plus `headroom`. It forks its lineage's seed, or boots cold from the image when the lineage has none. Each VM gets its own single-job JIT credential, minted for it by name.
- A green default-branch job's VM is snapshotted and swapped in as its lineage's seed. Only default-branch successes promote, so PR state never enters a seed.
- A finished runner, whose one-job registration is gone, is deleted.
- A new `image` makes every seed of the old image read as absent. Each lineage re-seeds from its next green run.
- An idle standby past `idle-grace-seconds` is deregistered and deleted. GitHub refuses (422) to deregister a runner mid-job, and that refusal is the only lock in the system.
- A tick that could not read the queue makes no scale-down decision, and an event tick only adds capacity.

One VM's failure logs an Actions error and the run continues. The job summary has a table of what happened.

## Security model

- `IX_TOKEN` and `RUNNER_PAT` never reach a runner VM. The reconcile refuses to start unless `RUNNER_ENVIRONMENT` says GitHub-hosted. On GHES or ARC, `IX_RUNNERS_ALLOW_NON_HOSTED=1` accepts that risk and lets `GITHUB_API_URL` name your own https API base. Everywhere else the API base is pinned to `api.github.com`, because any earlier step can rewrite `GITHUB_API_URL`.
- No credentialed request follows a redirect, since a 30x would send the Authorization header to whatever host `Location` names.
- A runner VM holds only its own JIT config. It takes one job as the runner it names, and is moved out of the watched path before the job starts, so a spent credential never rides into a seed. It is masked in logs when minted.
- An expired or revoked `RUNNER_PAT` shows as HTTP 401, and the reconcile stops and says so.
- The VM is the isolation boundary, with no co-tenants and no shared caches. A PR job can poison only its own fork. Seeds descend only from default-branch runs, so gate who can push there.
- `token-source: ix` removes the PAT. The reconcile trades its OIDC identity (`permissions: id-token: write`) for a repo-scoped App installation token minted by ix. The repository comes from the token's signed claims, so it cannot be minted for a repo the run did not prove.
