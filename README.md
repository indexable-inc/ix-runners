# ix-runners

Self-hosted GitHub Actions runner pools on [ix](https://ix.dev) VMs.

Persistent machines: toolchains, package registries, and compile caches stay
warm across runs. Why that beats stateless runners - with numbers - is the
[ix CI blog post](https://ix.dev/blog/ci).

This repository is the mechanism, maintained by ix. Your repo keeps only
policy: which toolchains your jobs need, pool size, labels. Fixes and
platform workarounds land here and reach you as a one-line pin bump.

Runners are persistent, not ephemeral. That is the point, not an oversight:
if you are used to ARC's job-scoped pods, the trade you are making is
isolation between jobs for a warm cache. A member is disposable at the pool
level - anything wedged converges away on the next reconcile - but two jobs
that land on one member share its disk.

## Setup

1. Add two Actions secrets to your repo: `IX_TOKEN` (the ix account the VMs
   bill to) and `RUNNER_PAT` (fine-grained PAT, Administration read/write on
   the repo).

   The built-in `GITHUB_TOKEN` cannot stand in for the PAT: workflow
   permissions have no `administration` scope, so it structurally cannot mint
   runner registration tokens. The PAT goes away with the ix GitHub App (#5).

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
   (which defaults to your repository's name). The module derives runner
   daemon names from it and the reconcile matches on those names, so if they
   disagree every member reads offline, gets repaired once, and is then
   replaced - forever. The pool destroys and rebuilds itself and never
   converges.

4. Add the workflow below and merge.

A complete consumer is four files, ~190 lines; all inputs are documented in
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
      - uses: actions/checkout@v7
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

Then swap `runs-on:` to `[self-hosted, ix]` in your other workflows at your
leisure.

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
  job one retry.
- Offline: restarted once, replaced if still offline next run.
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

## Security model

- `IX_TOKEN` and `RUNNER_PAT` live in GitHub Actions secrets and never
  reach a runner VM.
- The only credential a VM ever holds is a registration token. For its
  one-hour life that token can register a runner against your repo and steal
  its jobs - it is short-lived, not harmless. It is masked in Actions logs
  and deleted from the ix secret store at the end of the run that minted it.
- A `RUNNER_PAT` that has expired or been revoked presents as HTTP 401; the
  reconcile stops and says exactly that, so rotate the secret rather than
  hunting a status code.
- Machines are disposable by design and rev-anchored: a hand-edited or
  wedged VM converges away on the next reconcile.
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

## Development

```
uv run --no-project python -m unittest discover -s . -v
```

The suite fakes the ix SDK and the GitHub API, so it needs no credentials
and no dependencies - only a Python.

## Roadmap

- An official ix GitHub App with token vending through the ix API replaces
  `RUNNER_PAT`; setup becomes install-app plus one secret (#5).
- v2 is an ix-hosted control plane: webhook-driven ephemeral runners booted
  from warm snapshots. The workflow file in consumer repos deletes; the
  policy file stays.
