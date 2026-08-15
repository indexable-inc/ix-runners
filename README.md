# ix-runners

Self-hosted GitHub Actions runner pools on [ix](https://ix.dev) VMs.
Persistent machines, so toolchains, registries, and compile caches stay warm
across runs - that is the entire pitch versus stateless runners.

This repository is the **mechanism**, maintained by ix and shared by every
repo that runs CI on ix. Your repo keeps only **policy**: which toolchains
your jobs need, pool size, labels. Platform quirks and their workarounds live
here, tracked on this repo's issues, and fixes reach you as a rev bump of
one flake input.

## Setup (the whole of it)

1. Add two Actions secrets to your repo: `IX_TOKEN` (the ix account the VMs
   bill to) and `RUNNER_PAT` (fine-grained PAT, Administration rw on the
   repo - it never leaves GitHub-hosted runners).

2. Add the flake input and pool to your `flake.nix`:

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

3. Write your policy in `nix/ci-runner.nix` (~40 lines): `services.ix-runner`
   with your repo URL, pool name, and the toolchain packages your jobs
   expect on PATH.

4. Add a workflow that runs this repo's action on a schedule and on pushes
   touching the runner config, then dispatch it once with
   `max-replacements` = your pool size. Runners appear; swap `runs-on:` to
   `[self-hosted, ix]` at your leisure.

## Security model

- The reconcile runs on GitHub-hosted runners only. `IX_TOKEN` and
  `RUNNER_PAT` never reach a runner VM.
- At create, a short-lived (1 h) registration token is placed in the ix
  secret store (API body, never argv) and attached as a root-only file
  present at first boot. No post-boot seeding; nothing durable on any VM.
- Each image bakes the git rev it was built from; the reconciler replaces
  any member whose rev drifts from the last commit touching the runner
  config. Hand-edited or stale VMs converge away automatically.
- A member whose runners are mid-job is never replaced by a config roll; it
  is deferred until idle.

## Roadmap

- ~~Drop the ix CLI~~ (#3): done - provisioning is pure `ix-sdk`, including
  server-side first-boot template builds. Nothing is installed by `curl`.
- An official ix GitHub App + token vending via the ix API (#5) replaces
  `RUNNER_PAT`: customer setup becomes install-app + one secret.
- The scheduled-reconcile model is the v1. The v2 is an ix-hosted control
  plane (webhook-driven, ephemeral runners booted from warm snapshots), at
  which point the workflow file in consumer repos deletes too.
