# ix-runners

Self-hosted GitHub Actions runner pools on [ix](https://ix.dev) VMs.

[ix CI](https://ix.dev/blog/ci).

This repository is the mechanism for a nix github action runner, maintained by ix. 

Runners are persistent, not ephemeral

## Setup

1. Add two Actions secrets to your repo: `IX_TOKEN` (the ix account the VMs
   bill to) and `RUNNER_PAT` (fine-grained PAT, Administration read/write on
   the repo).

   The built-in `GITHUB_TOKEN` cannot stand in for the PAT: workflow
   permissions have no `administration` scope, so it structurally cannot mint
   runner registration tokens.

2. Wire the pool into your `flake.nix`:

   ```nix
   inputs.nixpkgs-ci.url = "github:NixOS/nixpkgs/nixos-unstable";
   inputs.ix-runners.url = "github:indexable-inc/ix-runners/<rev>";

   # in outputs:
   nixosConfigurations = ix-runners.lib.mkPool {
     nixpkgs = nixpkgs-ci;
     configRev = self.rev or null;
     modules = [ ./nix/ci-runner.nix ];
   };
   ```

3. Write your policy in `nix/ci-runner.nix`: `services.ix-runner` with your
   repo URL, a pool name, and the packages your jobs expect on PATH.

   `services.ix-runner.poolName` MUST equal the action's `pool-name` input
   (which defaults to your repository's name). 
   The module derives runner daemon names from it and the reconcile matches on those names, so if they
   disagree every member reads offline, gets repaired once, and is then replaced. 

4. Add the workflow below and merge.

[`action.yml`](./action.yml).

### The workflow

```yaml
name: ix runners

on:
  schedule:
    # Best effort: GitHub drops scheduled runs under load, and disables the
    # schedule entirely after 60 days with no commit to the repo.
    - cron: "*/30 * * * *"
  workflow_dispatch:
  push:
    branches: [main]
    # The reconcile's desired state is the last commit touching these paths,
    # so these are the only pushes that can change anything.
    paths:
      - nix/**
      - flake.nix
      - flake.lock

permissions:
  contents: read

# One reconcile at a time, and never cancel one mid-create: a cancelled run
# can leave a VM created but not yet registered.
concurrency:
  group: ix-runners
  cancel-in-progress: false

jobs:
  reconcile:
    # GITHUB-HOSTED only. A runner VM must never see IX_TOKEN or RUNNER_PAT.
    runs-on: ubuntu-latest
    steps:
      # Pinned by commit, not by tag: this job holds IX_TOKEN and a
      # repo-admin PAT, and checkout runs before the reconcile does - it can
      # rewrite the environment the reconcile then reads, through $GITHUB_ENV.
      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7
        with:
          # The desired rev is the last commit touching the runner config, so
          # the whole history has to be here. Under a shallow checkout the
          # grafted boundary commit diffs against the empty tree, every commit
          # looks like a config change, and the fleet rolls on every push. The
          # reconcile detects this and refuses to run rather than roll.
          fetch-depth: 0
          persist-credentials: false

      - uses: indexable-inc/ix-runners@<rev>
        with:
          ix-token: ${{ secrets.IX_TOKEN }}
          runner-pat: ${{ secrets.RUNNER_PAT }}
          pool-size: "8"
```

Then swap `runs-on:` to `[self-hosted, ix]` in your other workflows at your leisure.

## How it works

A scheduled reconcile - on GitHub-hosted runners, never on the pool it
manages - converges reality to your git history: the last commit touching
the runner config is the desired state, every VM image bakes the rev it was
built from, and any member that drifts is replaced. Creation goes through
the ix SDK; templates compile server-side on first boot and cache by rev.

- Missing member: created, with a fresh 1-hour registration token attached
  as a root-only file at first boot. No post-boot seeding step exists.
- Still booting: skipped. A first boot of a new rev compiles the template
  in-guest, and a machine that young has nothing to say about its health.
- Stale rev: replaced. Every runner slot's `busy` flag is checked on a
  freshly-read listing first, and all of the member's registrations are
  deleted before the VM is - GitHub refuses (422) to delete a runner that is
  mid-job. That closes the wide window, not all of it: a job assigned in the
  seconds between the listing and the delete is still lost, and costs that
  job one retry. A member that is busy at every single scan defers its own
  replacement indefinitely; past 30 days the run says so and asks you to
  drain it by hand, rather than killing a job to make a point.
- Offline: restarted once, replaced if still offline next run. A member
  started in the last few minutes is left alone instead - it reports
  `Running` from the moment it comes up, so "no runner yet" is what a
  healthy boot looks like.
- Reported failed by the platform: replaced at once, without waiting out the
  boot grace. The grace exists because a young machine's *silence* proves
  nothing; a machine saying it is dead is not silent.
- Stopped: left alone, and never probed. Its power state is read off the
  machine row, so a parked member is never mistaken for a dead one.
- Above `pool-size`: deregistered and deleted. Shrinking the pool would
  otherwise leave orphans billing and taking jobs.
- Empty pool: the per-run replacement cap self-raises to `pool-size`, so a
  first bootstrap builds every member in one run. If that run only partly
  succeeds the pool is no longer empty, the cap drops back to
  `max-replacements`, and the rest trickle in over the following runs.
- Creations and replacements execute concurrently (bounded, one registration
  token per run): a full-pool roll takes minutes, not one boot at a time.

Failures are per member. One member's failure is logged as an Actions error,
spends its budget, and the run continues; the job summary carries a table of
what happened to each member.

## Autoscaling

The member set never moves. `pool-size` machines exist, they are named and
built from your config, and the only thing autoscaling changes is which of
them are switched on. There is no scheduler here, nothing elastic, and
nothing that looks like Kubernetes: a stopped machine keeps its disk, so it
keeps its runner registration credentials and re-registers by itself on
boot, and while it is off it bills storage and nothing else.

Demand is GitHub's own queue - the jobs, queued or running, whose `runs-on`
includes `runner-label`. Each reconcile computes:

```
desired_online = clamp(ceil(jobs / slots) + scale-headroom,
                       min-warm, max-online)
```

Below that number, stopped members are started - always before creating
anything, because a start is a boot and a create is a template build. Above
it, idle members are stopped, highest index first, so the warm core is
always the same low-numbered machines and their caches stay hot. Nothing
busy is ever stopped: the decide pass skips it, and the execute pass re-reads
that member's registrations moments before pulling the power. That window is
narrowed, not closed, which is the same trade the replace path makes.

`min-warm` defaults to `pool-size`, so **autoscaling is off until you dial
it down**, and an unconfigured pool behaves exactly as it did before and
never even reads the queue. Turning it on:

```yaml
with:
  pool-size: 32
  min-warm: 3           # always-on floor: a small wave starts instantly
  scale-headroom: 2     # keep 2 spare above current demand
  max-online: 32
  runner-label: ix      # the demand signal; must be one of your labels
```

Two things carry the latency. `min-warm` means the front of a wave never
waits for a boot at all, and the GitHub queue absorbs the rest: a job that
finds no free runner waits, which is what a queue is for. A `workflow_run`
trigger on the reconcile turns a wave into a wake-up, so the pool is already
starting while the first jobs run - worst case is one boot, once per wave,
not once per job.

Reading the queue needs `permissions: actions: read` on the reconcile job
and the workflow's own `GITHUB_TOKEN`. The admin PAT is deliberately not
used for it: minting registration tokens and deleting runners is one job,
and reading a job list is another. If the read fails, the run says so and
keeps every member on - no view of the queue is never a reason to park the
pool.

Knobs, all optional: `min-warm`, `max-online`, `scale-headroom`,
`runner-label`, `idle-grace-ticks` (consecutive idle scans before a stop,
counted on the VM so it survives across runs), `max-power-actions` (per-run
cap on starts and stops, so a wrong decision converges slowly rather than
moving the fleet at once).

## Security model

- `IX_TOKEN` and `RUNNER_PAT` live in GitHub Actions secrets and never
  reach a runner VM. The reconcile refuses to start unless
  `RUNNER_ENVIRONMENT` says it is on a GitHub-hosted runner: it is the
  control plane for the pool, so running it on the pool would hand both
  secrets to the machines they exist to control. On GHES or ARC, set
  `IX_RUNNERS_ALLOW_NON_HOSTED=1` to accept that explicitly - which also
  lets `GITHUB_API_URL` name your own https API base. Everywhere else the
  API base is pinned to `api.github.com`, because `GITHUB_API_URL` is an
  environment variable any earlier step in the job can rewrite.
- No PAT-bearing request follows a redirect. urllib re-sends the
  Authorization header across a 30x, so one redirect would be enough.
- The only credential a VM ever holds is a registration token. For its
  one-hour life that token can register a runner against your repo and steal
  its jobs - it is short-lived, not harmless. It is masked in Actions logs
  and deleted from the ix secret store at the end of the run that minted it.
- A `RUNNER_PAT` that has expired or been revoked presents as HTTP 401; the
  reconcile stops and says exactly that, so rotate the secret rather than
  hunting a status code.
- Machines are disposable by design and rev-anchored: a hand-edited or
  wedged VM converges away on the next reconcile.
- A pool VM is the least trusted party in the reconcile. It is asked one
  question (which rev it was built from) and its answer is bounded and
  read as a string; a member that floods, hangs, or fails in any way is
  decided as unreachable and replaced, and can never end the run or stall
  the other members' convergence.
- The runners are persistent and shared across jobs: any job that runs on
  the pool owns the machine and its warm state (caches, toolchains) until the
  reconcile replaces it. Point only trusted events at the pool's labels -
  never `pull_request` from forks on a public repo, and gate
  `workflow_dispatch`-driven runs the same way.
- Persistent-not-ephemeral is deliberate: warm toolchains and compile caches
  are the product. If you want per-job isolation (ARC-style ephemeral
  runners), this is not that tool.
- Everything that runs your CI is in this repository, readable.

## What differs from ubuntu-latest

The runner VM is NixOS, tuned for parity where it is cheap and honest where
it is not:

- Foreign dynamically linked binaries (rustup/mise toolchains, prebuilt
  node, playwright browsers) run via nix-ld + envfs with a generous library
  set; a missing library fails at load time - file an issue, additions are
  one line.
- No sudo: the job user cannot elevate (`NoNewPrivileges`). Install into
  `$HOME` or ship the package in the pool's nix policy instead.
- `$HOME` is per-slot and persists across jobs and reboots; the checkout
  directory is wiped on every runner restart.
- Preinstalled tooling comes from the pool's nix policy, not from a hosted
  image: anything a job expects "to just be there" (Go, docker, protoc)
  must be listed there.

## Roadmap

- An official ix GitHub App with token vending through the ix API replaces
  `RUNNER_PAT`; setup becomes install-app plus one secret (#5).
- v2 is an ix-hosted control plane: webhook-driven ephemeral runners booted
  from warm snapshots. The workflow file in consumer repos deletes; the
  policy file stays.
