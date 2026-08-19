/** Every GitHub REST call, and the matching rules that read its answers.
 *
 * The admin credential never leaves this module's callers by accident: every
 * request refuses redirects (a 30x would re-aim the Authorization header at
 * whatever host Location names) and the API host is pinned rather than taken
 * from the environment, which any earlier step can rewrite via $GITHUB_ENV. */

import { clean, logError, logWarning } from "./report.ts"
import type { DemandedJob, FinishedJob, QueueObservation, Registration } from "./types.ts"
import { canonicalLabels } from "./names.ts"

/** Ceiling on active workflow runs whose jobs are counted for demand. */
const MAX_DEMAND_RUNS = 100
/** Hard cap on recently-completed runs scanned for promotions and idle
 * evidence. This window IS a held winner's promotability: once its run
 * scrolls past it, the machine can never be promoted and falls to the
 * idle-grace backstop. The scan normally stops far earlier, at the
 * caller's evidence floor (no run older than the oldest standing machine
 * can matter) - the cap only bounds the pathological wide-open case
 * against the workflow token's request budget. */
const FINISHED_SCAN_RUNS = 100
/** Spacing between registration DELETEs: rapid ones trip the secondary rate
 * limit, whose 422 wears the same status as a busy runner's refusal. */
const DEREGISTER_PAUSE_MS = 1000

const GITHUB_API_DEFAULT = "https://api.github.com"

/** The REST base, pinned to api.github.com. GHES deployments - exactly the
 * ones that already opted out of the hosted-runner guard - get theirs
 * honored, https only. */
function apiBase(): string {
  if (process.env.IX_RUNNERS_ALLOW_NON_HOSTED === "1") {
    return (process.env.GITHUB_API_URL ?? GITHUB_API_DEFAULT).replace(/\/+$/, "")
  }
  return GITHUB_API_DEFAULT
}

export class GitHub {
  constructor(
    /** Administration read/write: runner listing, JIT mint, deregister. */
    private readonly admin: string,
    /** The workflow's own token: reads the queue, nothing else. */
    private readonly workflow: string,
    private readonly repo: string,
  ) {}

  private async call(
    token: string,
    path: string,
    init: { method?: string; body?: unknown } = {},
    { fatalOn401 = false } = {},
  ): Promise<unknown> {
    const base = apiBase()
    const url = `${base}/repos/${this.repo}${path}`
    if (!url.startsWith(`${base}/`) || !base.startsWith("https://")) {
      logError(`refusing to send a credential to ${url}`)
      process.exit(1)
    }
    const response = await fetch(url, {
      method: init.method ?? "GET",
      headers: {
        authorization: `Bearer ${token}`,
        accept: "application/vnd.github+json",
        ...(init.body !== undefined ? { "content-type": "application/json" } : {}),
      },
      ...(init.body !== undefined ? { body: JSON.stringify(init.body) } : {}),
      redirect: "manual",
      signal: AbortSignal.timeout(30_000),
    })
    if (response.status >= 300 && response.status < 400) {
      throw new Error(
        `GitHub answered ${init.method ?? "GET"} ${path} with HTTP ${response.status}, a` +
          " redirect. Refusing to follow it with the Authorization header attached.",
      )
    }
    if (response.status === 401 && fatalOn401) {
      logError(
        "the admin credential was rejected (HTTP 401): expired or revoked." +
          " On the App path, check the ix-runners App is installed with" +
          " Administration read/write; on the PAT path, mint a fresh one.",
      )
      process.exit(1)
    }
    if (!response.ok) {
      // The body answered a credentialed request: secret-tainted, never logged.
      throw new HttpError(response.status, await response.text().catch(() => ""), path)
    }
    const text = await response.text()
    return text ? JSON.parse(text) : {}
  }

  /** Every self-hosted runner on the repo, across ALL pages, or refuse.
   *
   * A short read is destructive, not merely incomplete: an unlisted runner
   * machine reads as finished and gets deleted. */
  async listRunners(): Promise<Registration[]> {
    const runners: unknown[] = []
    let total: number | undefined
    for (let page = 1; ; page++) {
      const body = (await this.call(
        this.admin,
        `/actions/runners?per_page=100&page=${page}`,
        {},
        { fatalOn401: true },
      )) as { total_count?: number; runners?: unknown[] }
      if (body.total_count !== undefined) total = body.total_count
      const batch = body.runners ?? []
      runners.push(...batch)
      if (batch.length === 0 || (total !== undefined && runners.length >= total)) break
    }
    if (total === undefined || runners.length < total) {
      logError(
        `runner listing is not trustworthy (${runners.length} of ${total ?? "?"}).` +
          " Refusing to reconcile: every unlisted runner would read finished" +
          " and be deleted.",
      )
      process.exit(1)
    }
    return runners.map((raw) => {
      const r = raw as { id: number; name: string; status: string; busy?: boolean }
      return { id: r.id, name: r.name, online: r.status === "online", busy: !!r.busy }
    })
  }

  /** Mint a single-job JIT runner bound to this repo.
   *
   * The blob is the runner's whole credential: the caller masks it and
   * writes it to exactly one machine. `self-hosted` is implicit and refused
   * by the endpoint, so it is stripped here. */
  async mintJitConfig(name: string, labels: readonly string[]): Promise<string> {
    const body = (await this.call(
      this.admin,
      "/actions/runners/generate-jitconfig",
      {
        method: "POST",
        body: {
          name,
          runner_group_id: 1,
          labels: labels.filter((label) => label !== "self-hosted"),
        },
      },
      { fatalOn401: true },
    )) as { encoded_jit_config?: string }
    if (!body.encoded_jit_config) throw new Error("generate-jitconfig answered without a config")
    return body.encoded_jit_config
  }

  /** Delete registrations; false when the runner is busy (GitHub's 422
   * refusal - the one real lock in this system). */
  async deregister(ids: readonly number[]): Promise<boolean> {
    for (const [index, id] of ids.entries()) {
      if (index > 0) await Bun.sleep(DEREGISTER_PAUSE_MS)
      try {
        await this.call(this.admin, `/actions/runners/${id}`, { method: "DELETE" })
      } catch (error) {
        if (error instanceof HttpError && error.status === 404) continue // already gone
        if (error instanceof HttpError && error.status === 422 && error.saysBusy()) return false
        throw error
      }
    }
    return true
  }

  /** One pass over the queue: demand for this pool, and the recently
   * finished jobs that drive promotion and idle evidence. Returns null when
   * the read failed: missing data is not zero demand, and the caller's
   * answer is to make no scale-down decision at all.
   *
   * `evidenceFloorMs` time-bounds the completed-runs scan: runs created
   * before it cannot carry evidence for any standing machine (the caller
   * derives it from the oldest standing runner), so the scan stops there
   * instead of always spending the full run budget against the workflow
   * token's rate limit. */
  async observeQueue(
    markerLabel: string,
    defaultBranch: string,
    evidenceFloorMs?: number,
  ): Promise<QueueObservation | null> {
    try {
      const demanded: DemandedJob[] = []
      const finished: FinishedJob[] = []
      // Shared by both loops: a completed job is promotion/deletion evidence
      // wherever it is seen. Jobs of still-active runs MUST feed this too: a
      // long multi-job run finishes (and deregisters) its early jobs many
      // minutes before the run itself completes, and a runner machine whose
      // evidence is invisible until run-completion would be reaped as
      // job-finished before it could ever be promoted into a seed.
      const collectFinished = (
        job: Awaited<ReturnType<typeof this.runJobs>>[number],
        trustedRun: boolean,
      ) => {
        const completedAt = Date.parse(job.completed_at ?? "")
        if (!job.runner_name || Number.isNaN(completedAt)) return
        finished.push({
          runnerName: job.runner_name,
          labels: canonicalLabels(job.labels ?? []),
          completedAt: completedAt / 1000,
          succeeded: job.conclusion === "success",
          // The branch name alone is forgeable (a fork PR's head branch can
          // be named after the default branch); the run's provenance must
          // vouch too, or fork code seeds the machines trusted jobs fork from.
          onDefaultBranch: job.head_branch === defaultBranch && trustedRun,
        })
      }
      let truncated = false
      for (const status of ["queued", "in_progress"] as const) {
        const { runs, hitCap } = await this.scanRuns(status, MAX_DEMAND_RUNS)
        truncated ||= hitCap
        // Per-run job reads are same-depth: one bounded batch, not a serial
        // walk - the event tick exists to cut pickup latency, not add it.
        const jobLists = await mapLimit(runs, JOB_READ_CONCURRENCY, (run) => this.runJobs(run.id))
        for (const [index, jobs] of jobLists.entries()) {
          const run = runs[index]!
          for (const job of jobs) {
            if (job.status === "completed") {
              collectFinished(job, run.trusted)
              continue
            }
            if (job.status !== "queued" && job.status !== "in_progress") continue
            const labels = canonicalLabels(job.labels ?? [])
            if (labels.includes(markerLabel)) demanded.push({ labels })
          }
        }
      }
      const { runs } = await this.scanRuns("completed", FINISHED_SCAN_RUNS, evidenceFloorMs)
      const jobLists = await mapLimit(runs, JOB_READ_CONCURRENCY, (run) => this.runJobs(run.id))
      for (const [index, jobs] of jobLists.entries()) {
        const run = runs[index]!
        for (const job of jobs) collectFinished(job, run.trusted)
      }
      return { demanded, finished, truncated }
    } catch (error) {
      const status = error instanceof HttpError ? error.status : undefined
      const hint =
        status !== undefined && [401, 403, 404].includes(status)
          ? " The workflow's token needs `permissions: actions: read`."
          : ""
      logWarning(
        `could not read the job queue (${status ?? clean(error)}).${hint}` +
          " Making no scale-down decision this tick.",
      )
      return null
    }
  }

  /** The repository's default branch: only jobs on it may promote a seed. */
  async defaultBranch(): Promise<string> {
    const body = (await this.call(this.workflow, "")) as { default_branch?: string }
    if (!body.default_branch) {
      logError("could not read the repository's default branch")
      process.exit(1)
    }
    return body.default_branch
  }

  /** The run listing, with the provenance the jobs API omits: the jobs
   * endpoint has no head_repository, so whether a run's code is this
   * repository's is only learnable here and must ride along to every job
   * it carries. `sinceMs` stops the scan at the first run created before
   * it - the listing is newest-first (GitHub's documented default order),
   * so nothing after that run can matter. */
  private async scanRuns(
    status: string,
    cap: number,
    sinceMs?: number,
  ): Promise<{ runs: RunMeta[]; hitCap: boolean }> {
    const runs = new Map<number, RunMeta>()
    let floored = false
    outer: for (let page = 1; runs.size <= cap; page++) {
      const body = (await this.call(
        this.workflow,
        `/actions/runs?status=${status}&per_page=100&page=${page}`,
      )) as {
        total_count?: number
        workflow_runs?: {
          id: number
          event?: string
          created_at?: string
          head_repository?: { full_name?: string }
        }[]
      }
      const batch = body.workflow_runs ?? []
      if (batch.length === 0) break
      for (const run of batch) {
        const createdAtMs = Date.parse(run.created_at ?? "")
        if (sinceMs !== undefined && createdAtMs < sinceMs) {
          floored = true
          break outer
        }
        runs.set(run.id, { id: run.id, trusted: trustedRunProvenance(run, this.repo) })
      }
      if (runs.size >= (body.total_count ?? 0)) break
    }
    return { runs: [...runs.values()].slice(0, cap), hitCap: !floored && runs.size > cap }
  }

  private async runJobs(runId: number): Promise<
    {
      status?: string
      conclusion?: string
      labels?: string[]
      runner_name?: string
      completed_at?: string
      head_branch?: string
    }[]
  > {
    const jobs: {
      status?: string
      conclusion?: string
      labels?: string[]
      runner_name?: string
      completed_at?: string
      head_branch?: string
    }[] = []
    for (let page = 1; ; page++) {
      const body = (await this.call(
        this.workflow,
        `/actions/runs/${runId}/jobs?filter=latest&per_page=100&page=${page}`,
      )) as { jobs?: { head_branch?: string }[] }
      const batch = (body.jobs ?? []) as typeof jobs
      jobs.push(...batch)
      if (batch.length < 100) break
    }
    return jobs
  }
}

/** One run from the runs listing, reduced to what the queue scan needs. */
interface RunMeta {
  readonly id: number
  /** The run's code provably came from this repository (see below). */
  readonly trusted: boolean
}

/** Whether a run's post-job disk state may seed the lineage every trusted
 * job forks from.
 *
 * `head_branch` alone is FORGEABLE: a fork pull request whose head branch
 * is also named "main" matches the default branch, and promoting it would
 * bake attacker-controlled disk state into the seed every subsequent job
 * forks from. Provenance must also say the code is this repository's: a
 * `push` event, or any event whose head repository IS the repository
 * (schedule and workflow_dispatch runs carry that; fork PRs carry the
 * fork's name). Repository names compare case-insensitively, as GitHub
 * treats them. */
export function trustedRunProvenance(
  run: { event?: string; head_repository?: { full_name?: string } },
  repo: string,
): boolean {
  if (run.event === "push") return true
  return run.head_repository?.full_name?.toLowerCase() === repo.toLowerCase()
}

/** GitHub documents 422 on runner-delete only as "validation failed or
 * spammed"; that a BUSY runner refuses with it is community knowledge, and a
 * rate-limit 422 wears the same code. Read the body before believing busy. */
const BUSY_REFUSAL = ["busy", "running a job", "job is still running"]

export class HttpError extends Error {
  constructor(
    readonly status: number,
    /** Secret-tainted (answered a credentialed request): classified, never logged. */
    private readonly body: string,
    path: string,
  ) {
    super(`HTTP ${status} on ${path}`)
  }

  saysBusy(): boolean {
    const lowered = this.body.toLowerCase()
    return BUSY_REFUSAL.some((hint) => lowered.includes(hint))
  }
}

/** GitHub tolerates modest read parallelism; 8 keeps the worst-case scan
 * (~300 runs) to a few dozen batched round trips without brushing the
 * secondary rate limit the way a burst of hundreds would. */
const JOB_READ_CONCURRENCY = 8

/** `items -> fn` with at most `limit` in flight, order-preserving. */
async function mapLimit<T, R>(
  items: readonly T[],
  limit: number,
  fn: (item: T) => Promise<R>,
): Promise<R[]> {
  const results: R[] = new Array(items.length)
  let next = 0
  await Promise.all(
    Array.from({ length: Math.min(limit, items.length) }, async () => {
      while (next < items.length) {
        const index = next++
        results[index] = await fn(items[index]!)
      }
    }),
  )
  return results
}
