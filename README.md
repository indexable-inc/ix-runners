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

2. Describe the pool once, in `nix/ix-pool.json`:

   ```json
   {
     "pool-name": "myrepo",
     "region": "us-east-1",
     "pool-size": 8
   }
   ```

   Every key is optional and defaults in one place; the file's presence is
   what declares that this repo has a pool. It lives under `nix/` because
   that directory already defines the runner config rev, so changing the
   pool's shape rolls the fleet like any other config change.

   Unknown keys are an error, not a default. A typo that silently defaults
   is a pool quietly running someone else's numbers - `mniWarm` would read
   as "autoscaling off" and the only symptom is the bill.

3. Wire it into your `flake.nix`:

   ```nix
   inputs.nixpkgs-ci.url = "github:NixOS/nixpkgs/nixos-unstable";
   inputs.ix-runners.url = "github:indexable-inc/ix-runners/<rev>";

   # in outputs:
   nixosConfigurations = ix-runners.lib.mkPool {
     nixpkgs = nixpkgs-ci;
     configRev = self.rev or null;
     spec = nixpkgs.lib.importJSON ./nix/ix-pool.json;
     modules = [ ./nix/ci-runner.nix ];
   };
   ```

   The flake and the reconcile now read the same file, so the pool's size
   cannot be two different numbers in two places. It used to be exactly
   that, with a comment asking you to keep them equal; a larger size in the
   workflow asked for flake attrs that did not exist and every run was red
   until somebody noticed.

4. Write your policy in `nix/ci-runner.nix`: `services.ix-runner` with your
   repo URL and the packages your jobs expect on PATH. The pool name comes
   from the spec, so do not set `poolName` here as well.

5. Add the workflow below and merge.

### The whole invocation

```yaml
- uses: indexable-inc/ix-runners@<rev>
  with:
    ix-token: ${{ secrets.IX_TOKEN }}
    runner-pat: ${{ secrets.RUNNER_PAT }}
```

Two secrets. The pool's name, size, region and every autoscaling dial come
from the spec file; `config-file` moves it off the default path if you must.
There is deliberately no way to set a pool's size on the action.

### The spec

| key | default | |
| --- | --- | --- |
| `pool-name` | the repository's name | VM names `<pool>-runner-<N>`, runner names `<pool>-r<N>-<slot>` |
| `region` | `us-west-1` | ix region the pool lives in |
| `pool-size` | 8 | members, and the flake attrs `mkPool` generates |
| `attr-prefix` | `ci-runner` | flake attribute prefix for members |
| `max-replacements` | 2 | per-run cap on creations + replacements |
| `concurrency` | 4 | creations/replacements executed at once |
| `runner-label` | — | bootstrap demand match; required once `min-warm` < `max-online` |
| `min-warm` | `pool-size` | always-on floor. **This is the autoscaling on/off switch** |
| `max-online` | `pool-size` | ceiling on powered-on members |
| `scale-headroom` | 2 | spare members kept above current demand |
| `idle-grace-seconds` | 600 | idle time before a member is switched off |
| `max-stops` | 4 | per-tick cap on stops; starts are uncapped |

`mkPool` asserts at build time that `runner-label` is actually in
`services.ix-runner.labels`. A label nothing advertises means the pool never
wakes for a wave, and zero servable jobs looks exactly like zero jobs.

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
  # Only needed once you turn autoscaling on: it is how the demand signal
  # reads the job queue. Without it the queue read is refused, the run warns,
  # and the pool stays fully on - so a pool that never scales down is the
  # first thing to check here.
  actions: read

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
  machine row, so a parked member is never mistaken for a dead one. Waking
  one re-registers it; see Autoscaling for why it cannot simply reconnect.
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

The member set never moves. `pool-size` machines exist, named and built from
your config, and the only thing autoscaling changes is which of them are
switched on. There is no scheduler here and nothing that resembles
Kubernetes: a stopped machine keeps its disk, and while it is off it bills
storage and nothing else.

It is **level-based and stateless**. Every tick observes the world fresh -
the machine rows, the runner registrations, GitHub's job queue - computes a
level, and moves toward it. Nothing is remembered between runs: no idle
counters, no pending-action list, no event log to replay. A missed tick
costs latency, never correctness, and two reconciles racing cannot disagree
about what the pool looked like.

```
desired_online = clamp(ceil(servable_jobs / slots) + scale-headroom,
                       min-warm, max-online)
```

**Servable is strict.** A job counts only when some runner in this pool
advertises *every* label in its `runs-on` - GitHub ANDs them, and it checks
them against one runner, so two machines cannot combine to cover a job. This
is not a detail. Repos routinely have jobs queued indefinitely against
labels nothing in the pool carries (`blacksmith-*`, `macos-*`, hosted-only
labels): work that will never be served from here. Counting it is not
conservative, it is a permanent maximum, and the pool would sit at
`max-online` forever running nothing. Labels are read off the runners' own
registrations, so what the pool advertises and what it is matched on cannot
drift apart.

**The GitHub queue is the buffer.** A job that finds no free runner waits,
which is what a queue is for. Below the level, stopped members start -
always before anything is created, since a start is a boot and a create is a
template build, and never rate-limited, because being short of capacity is
the state with a queue behind it. Above the level, idle members stop,
highest index first, so the warm core is a stable set of low-numbered
machines whose caches stay hot.

Two rules stop a wave being throttled by its own arrival:

- **Only a scheduled tick may scale down.** An event tick fires when a run is
  *requested*, before its jobs reach the queue, so it sees an idle pool at
  the exact moment a wave is landing. Event ticks may only add capacity.
- **A tick that cannot read GitHub makes no scaling decision at all.**
  Missing data is not zero demand, and it is not zero idleness either -
  guessing up costs money forever, guessing down stops machines about to be
  handed a job. Healing still runs; only scaling is skipped.

**Idle time is derived, not counted.** A member's idle clock is the last job
completion GitHub recorded against its runners. No stored counter, nothing
to reset, nothing that can fall out of step with reality. A member the scan
never saw finish anything is idle only as far back as the scan's own window
reaches, which is why a very busy repo - where that window is shorter than
the grace - simply does not scale down that tick.

**Scale-down deregisters before it cuts power.** GitHub refuses (422) to
delete a runner that is mid-job, and that refusal is the only real lock in
this system: once the registration is gone the runner cannot be assigned
anything, which is a guarantee no amount of re-reading a listing can give.
Checking `busy` and then stopping always leaves a window where a job is
assigned and then killed. This closes it.

The price is that a stop is no longer free to undo. The registration is
gone, so waking a member means minting a fresh registration token,
overwriting the pool's secret, and letting the platform deliver it - the
machine re-registers at boot precisely because the token changed. Two
consequences worth knowing:

- The secret row is **never deleted**. A rotation only propagates to
  machines already holding a copy when the write *updates* an existing row;
  a write that inserts is a first write and reaches nobody. Deleting the
  spent token would make every subsequent write an insert, and no stopped
  member would ever receive a usable token again.
- A rotation reaches every member, running ones included, where it lands as
  a new token file. Those members re-register on their next unit restart,
  which is fine while the token is fresh and fails once it has expired. The
  reconcile mints in the same tick as any repair, so a restart it drives is
  always holding a new token; an unplanned reboot long after the last
  rotation can need one repair cycle to come back.

`min-warm` defaults to `pool-size`, so **autoscaling is off until you dial
it down**, and an unconfigured pool behaves exactly as it did before and
never even reads the queue. Turning it on is four keys in `nix/ix-pool.json`:

```json
{
  "pool-name": "myrepo",
  "region": "us-east-1",
  "pool-size": 32,
  "runner-label": "ix",
  "min-warm": 3,
  "scale-headroom": 2,
  "max-online": 32
}
```

Reading the queue needs `permissions: actions: read` and the workflow's own
`GITHUB_TOKEN`. The admin PAT is deliberately not used for it: minting
registration tokens and deleting runners is one job, reading a job list is
another.

Every tick prints one `DECISION` line - observed counts, the level they
imply, and the actions taken - so a reader can reconstruct what happened
without replaying the log.

Remaining knobs, all optional and all in the same file: `idle-grace-seconds`
(default 600) and `max-stops` (per-tick cap, default 4; starts are
uncapped). Which ticks may scale down is NOT configurable - the trigger
already says, and pinning it in a file would pin it for the cron too.

### Where this is going

This is v1, and it is deliberately the simple thing: long-lived machines
whose power state follows a level. The end state is ephemeral runners - a
machine per job, booted from a warm snapshot on a webhook and destroyed
when the job ends - which removes the idle question entirely rather than
managing it. That needs an ix-hosted control plane (see the roadmap); until
it exists, a fixed pool with a power dial is the version that is honest
about what it can guarantee.

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
