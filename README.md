# ix-runners

Self-hosted GitHub Actions runner pools on [ix](https://ix.dev) VMs.

Persistent machines: toolchains, package registries, and compile caches stay
warm across runs. Why that beats stateless runners - with numbers - is the
[ix CI blog post](https://ix.dev/blog/ci).

This repository is the mechanism, maintained by ix. Your repo keeps only
policy: which toolchains your jobs need, pool size, labels. Fixes and
platform workarounds land here and reach you as a one-line pin bump.

## Setup

1. Add two Actions secrets to your repo: `IX_TOKEN` (the ix account the VMs
   bill to) and `RUNNER_PAT` (fine-grained PAT, Administration read/write on
   the repo).

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

4. Add a workflow that runs this repo's action on a schedule and on pushes
   touching the runner config. Merge. The first reconcile builds the whole
   pool; swap `runs-on:` to `[self-hosted, ix]` at your leisure.

A complete consumer is four files, ~190 lines; see the action inputs in
[`action.yml`](./action.yml).

## How it works

A scheduled reconcile - on GitHub-hosted runners, never on the pool it
manages - converges reality to your git history: the last commit touching
the runner config is the desired state, every VM image bakes the rev it was
built from, and any member that drifts is replaced. Creation goes through
the ix SDK; templates compile server-side on first boot and cache by rev.

- Missing member: created, with a fresh 1-hour registration token attached
  as a root-only file at first boot. No post-boot seeding step exists.
- Stale rev: replaced, never under a running job - registrations are
  deleted first, and GitHub's refusal to delete a busy runner is the lock.
- Offline: restarted once, replaced if still offline next run.
- Empty pool: the per-run replacement cap self-raises, so first bootstrap
  is just the first tick.

## Security model

- `IX_TOKEN` and `RUNNER_PAT` live in GitHub Actions secrets and never
  reach a runner VM.
- The only credential a VM ever holds is a registration token that expires
  in an hour and can do nothing but register a runner.
- Machines are disposable by design and rev-anchored: a hand-edited or
  wedged VM converges away on the next reconcile.
- Everything that runs your CI is in this repository, readable.

## Roadmap

- An official ix GitHub App with token vending through the ix API replaces
  `RUNNER_PAT`; setup becomes install-app plus one secret (#5).
- v2 is an ix-hosted control plane: webhook-driven ephemeral runners booted
  from warm snapshots. The workflow file in consumer repos deletes; the
  policy file stays.
