# ix-runners

Self-hosted GitHub Actions runner pools on [ix](https://ix.dev) VMs,
maintained by ix. Your repo declares a pool in one JSON file and adds a
two-line action; this repository is the mechanism that creates the machines,
keeps them healthy, and switches them on and off to follow your job queue.

Runners are **persistent, not ephemeral** - warm toolchains and compile
caches are the point. If you need per-job isolation, this is not that tool.

## Quickstart

Ten minutes, four steps.

**1. Two Actions secrets.** `IX_TOKEN` (the ix account the VMs bill to) and
`RUNNER_PAT` (a fine-grained PAT with Administration read/write on the repo).
The built-in `GITHUB_TOKEN` cannot stand in for the PAT: workflow permissions
have no `administration` scope, so it structurally cannot mint runner
registration tokens.

**2. Describe the pool** in `nix/ix-pool.json`:

```json
{
  "pool-name": "myrepo",
  "region": "us-east-1",
  "pool-size": 8
}
```

Every key is optional and defaults in one place; the file's presence is what
declares that this repo has a pool. Unknown keys are an error, not a default.

**3. Wire it into `flake.nix`:**

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

Write your policy in `nix/ci-runner.nix`: `services.ix-runner` with your repo
URL, the labels your jobs will target, and the packages they expect on PATH.
The pool's name comes from the spec, so do not set `poolName` there too.

**4. Add the workflow and merge:**

```yaml
name: ix runners

on:
  schedule:
    - cron: "*/30 * * * *"
  workflow_dispatch:
  # Wake the pool when a run is queued, so it warms while the first jobs run.
  workflow_run:
    workflows: ["CI"]          # your own workflow names
    types: [requested]
  push:
    branches: [main]
    # The desired state is the last commit touching these paths.
    paths: [nix/**, flake.nix, flake.lock]

permissions:
  contents: read
  actions: read                # reads the job queue for the demand signal

# One reconcile at a time, and never cancel one mid-create.
concurrency:
  group: ix-runners
  cancel-in-progress: false

jobs:
  reconcile:
    # GITHUB-HOSTED only. A runner VM must never see IX_TOKEN or RUNNER_PAT.
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7
        with:
          fetch-depth: 0       # a shallow checkout is refused, not guessed at
          persist-credentials: false

      - uses: indexable-inc/ix-runners@<rev>
        with:
          ix-token: ${{ secrets.IX_TOKEN }}
          runner-pat: ${{ secrets.RUNNER_PAT }}
```

`schedule` and `workflow_run` only fire from a workflow file on your
**default branch** - merging is what starts the pool.

Then swap `runs-on:` to `[self-hosted, ix]` in your other workflows.

## How it behaves

A reconcile runs on GitHub-hosted runners - never on the pool it manages -
and converges the pool to your git history. The last commit touching `nix/`,
`flake.nix` or `flake.lock` is the desired rev; every VM bakes the rev it was
built from, and any member that drifts is replaced.

### Member lifecycle

| member state | what happens |
| --- | --- |
| missing | created, with a 1-hour registration token present at first boot |
| created in the last 30 min | skipped - it is still compiling its template |
| started in the last 5 min | skipped - "no runner yet" is what a healthy boot looks like |
| on a stale rev | deregistered, then replaced. Deferred while any of its runners is busy |
| runners offline | units restarted once; replaced if still offline next run |
| reported `failed` by ix | replaced at once, without waiting out the boot grace |
| stopped | left alone and never probed; the scaler decides whether to wake it |
| above `pool-size` | deregistered and deleted - a shrink's orphans keep billing |

Failures are per member: one member's failure is logged, its budget stays
spent, and the run continues. The job summary carries a table of what
happened to each member.

### Autoscaling

The member set never moves. `pool-size` machines exist, and the only thing
autoscaling changes is which are switched on. A stopped machine keeps its
disk and bills storage alone. There is no scheduler here.

```
desired_online = clamp(ceil(servable_jobs / slots) + scale-headroom,
                       min-warm, max-online)
```

Below the level, stopped members start - always before anything is created,
and never rate-limited. Above it, idle members stop, highest index first, so
the warm core is a stable set whose caches stay hot. The GitHub queue is the
buffer: a job that finds no free runner waits.

Three rules do most of the work:

- **Servable is strict.** A job counts only if some runner advertises *every*
  label in its `runs-on`. Jobs queued against labels your pool does not carry
  contribute nothing - otherwise a permanently-stuck queue pins the pool at
  maximum forever.
- **Event ticks only scale up.** A `workflow_run` tick fires before its jobs
  reach the queue, so it sees an idle pool exactly as a wave lands. Only the
  cron may switch machines off.
- **An unreadable queue makes no scaling decision.** Missing data is not zero
  demand and not zero idleness. Healing still runs.

Scale-down **deregisters before it cuts power** - GitHub's 422-on-busy is the
only real lock available, so a running job can never be stopped out from
under. The cost is that waking a member mints a fresh registration token.

`min-warm` defaults to `pool-size`, so **autoscaling is off until you dial it
down**, and an unconfigured pool never even reads the queue.

Reasoning, trade-offs and the failures behind each rule:
[docs/design.md](docs/design.md).

## Knobs

All of them live in `nix/ix-pool.json`. All optional.

| key | default | meaning |
| --- | --- | --- |
| `pool-name` | the repo's name | VM names `<pool>-runner-<N>`, runner names `<pool>-r<N>-<slot>` |
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

Which ticks may scale down is deliberately not configurable: the trigger
already says, and pinning it would pin it for the cron too.

## Operations

**Bootstrap.** Merge the workflow to your default branch and wait for the
first tick, or dispatch it. An empty pool raises the replacement cap to
`pool-size` on its own, so the first run builds every member at once; if it
only partly succeeds, the rest trickle in at `max-replacements` per run.

**Kill switch.** Set `min-warm` equal to `pool-size`. Scaling stops
immediately, every member that exists stays on, and no tick reads the queue.
Nothing else changes - healing continues.

**Reading a run.** Every tick prints one decision line:

```
DECISION [scheduled] powered_on=4 (online=4 warming=0 stopped=28) demand=6 -> desired=8 [6 servable job(s)/1 slot(s) = 6 +2 headroom, clamped [3,32]] | start [5, 6, 7, 8] stop []
reconcile done: 0 creation(s)/replacement(s), 4 power change(s), 0 failed
```

Observed counts, the level they imply, and what was done - enough to
reconstruct the decision without replaying the log. `demand=n/a` means the
queue could not be read and no scaling was attempted.

**The pool never scales down.** Check `permissions: actions: read` first - a
403 on the queue read warns and leaves every member on, and the run stays
green. Then check that `runner-label` is a label your runners actually
advertise.

**The pool never scales up.** Almost always the label rule: your jobs' full
`runs-on` set must be carried by one runner. `mkPool` asserts the spec's
`runner-label` is in `services.ix-runner.labels` at build time.

**Nothing runs at all.** `schedule` and `workflow_run` only fire from the
default branch. A workflow still on a feature branch never ticks.

## Security

- `IX_TOKEN` and `RUNNER_PAT` never reach a runner VM. The reconcile refuses
  to start unless `RUNNER_ENVIRONMENT` says GitHub-hosted - it is the control
  plane for the pool, so running it on the pool would hand both secrets to
  the machines they exist to control. On GHES/ARC set
  `IX_RUNNERS_ALLOW_NON_HOSTED=1` to accept that explicitly.
- The API base is pinned to `api.github.com` (`GITHUB_API_URL` is an
  environment variable any earlier step can rewrite), and no PAT-bearing
  request follows a redirect - urllib re-sends the Authorization header
  across a 30x, so one redirect would be enough.
- The only credential a VM holds is a registration token. For its one-hour
  life it can register a runner against your repo and steal its jobs: it is
  short-lived, not harmless. It is masked in Actions logs.
- The demand signal uses the workflow's own `GITHUB_TOKEN`, never the PAT:
  listing runs needs the Actions permission, and repo administration has no
  business holding one.
- An expired or revoked `RUNNER_PAT` presents as HTTP 401 and the reconcile
  says exactly that, so you rotate a secret rather than hunt a status code.
- A pool VM is the least trusted party here. It is asked one question - which
  rev it was built from - and its answer is bounded and read as a string. A
  member that floods, hangs or fails is decided as unreachable and replaced,
  and can never end the run or stall the other members.
- **Runners are shared across jobs.** Any job that runs on the pool owns the
  machine and its warm state until the reconcile replaces it. Point only
  trusted events at the pool's labels - never `pull_request` from forks on a
  public repo.
- Everything that runs your CI is in this repository, readable.

## Differences from ubuntu-latest

The runner VM is NixOS, tuned for parity where that is cheap and honest where
it is not:

- Foreign dynamically linked binaries (rustup/mise toolchains, prebuilt node,
  playwright browsers) run via nix-ld + envfs. A missing library fails at
  load time - file an issue, additions are one line.
- No sudo: the job user cannot elevate. Install into `$HOME`, or ship the
  package in the pool's nix policy.
- `$HOME` is per-slot and persists across jobs and reboots; the checkout
  directory is wiped on every runner restart.
- Preinstalled tooling comes from your nix policy, not a hosted image.
  Anything a job expects "to just be there" must be listed there.

## Roadmap

- An ix GitHub App with token vending replaces `RUNNER_PAT`; setup becomes
  install-app plus one secret (#5).
- v2 is an ix-hosted control plane: webhook-driven ephemeral runners from
  warm snapshots. The workflow file in consumer repos deletes; the policy
  file stays.
