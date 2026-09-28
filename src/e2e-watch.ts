/** The e2e watcher: drives one GitHub Actions job on an ix runner to a typed
 * outcome under local deadlines, and fails the workflow unless that job
 * started, went green, and started within budget.
 *
 * It exists because a `runs-on: [self-hosted, ...]` job with no runner does
 * not fail: it sits queued for 24 hours, and `timeout-minutes` only counts
 * from the moment a runner takes it. Without a watcher a broken reconcile
 * reads as "slow", never as "broken". On a missed start deadline this
 * cancels the run, so the stranded job cannot hold the concurrency group.
 *
 * Environment (all set by .github/workflows/e2e.yml):
 *   GITHUB_TOKEN        workflow token, `actions: write` (cancel on failure)
 *   GITHUB_REPOSITORY   owner/repo
 *   E2E_RUN_ID          run holding the job
 *   E2E_RUN_ATTEMPT     that run's attempt
 *   E2E_JOB_NAME        the job's display name
 *   E2E_START_DEADLINE_S   give up on a job still queued after this long
 *   E2E_FINISH_DEADLINE_S  give up on a job still running after this long
 *   E2E_START_BUDGET_S  optional: fail a green job whose queue-to-start
 *                       latency exceeded this (the speed assertion) */

import { appendFile } from "node:fs/promises"
import { clean, logError } from "./report.ts"

const API = "https://api.github.com"
const POLL_MS = 3_000

interface Step {
  readonly name: string
  readonly started_at: string | null
  readonly completed_at: string | null
  readonly conclusion: string | null
}

interface Job {
  readonly name: string
  readonly status: string
  readonly conclusion: string | null
  readonly created_at: string
  readonly started_at: string | null
  readonly completed_at: string | null
  readonly runner_name: string | null
  readonly steps?: readonly Step[]
}

type Outcome =
  | { readonly kind: "finished"; readonly job: Job }
  | { readonly kind: "never-queued"; readonly elapsedS: number; readonly polls: number }
  | { readonly kind: "never-started"; readonly elapsedS: number; readonly polls: number; readonly job: Job }
  | { readonly kind: "never-finished"; readonly elapsedS: number; readonly polls: number; readonly job: Job }

function required(name: string): string {
  const value = process.env[name]
  if (!value) {
    logError(`${name} is required`)
    process.exit(1)
  }
  return value
}

function seconds(name: string): number {
  const raw = required(name)
  const value = Number(raw)
  if (!Number.isFinite(value) || value <= 0) {
    logError(`${name} must be a positive number of seconds, got '${raw}'`)
    process.exit(1)
  }
  return value
}

const token = required("GITHUB_TOKEN")
const repo = required("GITHUB_REPOSITORY")
const runId = required("E2E_RUN_ID")
const attempt = required("E2E_RUN_ATTEMPT")
const jobName = required("E2E_JOB_NAME")
const startDeadlineS = seconds("E2E_START_DEADLINE_S")
const finishDeadlineS = seconds("E2E_FINISH_DEADLINE_S")
const startBudgetS = process.env.E2E_START_BUDGET_S ? seconds("E2E_START_BUDGET_S") : undefined

async function github(method: string, path: string): Promise<Response> {
  const response = await fetch(`${API}${path}`, {
    method,
    redirect: "error",
    headers: {
      authorization: `Bearer ${token}`,
      accept: "application/vnd.github+json",
      "x-github-api-version": "2022-11-28",
    },
  })
  return response
}

async function readJob(): Promise<Job | undefined> {
  const response = await github(
    "GET",
    `/repos/${repo}/actions/runs/${runId}/attempts/${attempt}/jobs?per_page=100`,
  )
  if (!response.ok) {
    throw new Error(`listing jobs of run ${runId}: HTTP ${response.status} ${await response.text()}`)
  }
  const body = (await response.json()) as { jobs: Job[] }
  return body.jobs.find((job) => job.name === jobName)
}

/** Poll until the job completes or a deadline passes. The start deadline
 * counts from the watcher's own start; the finish deadline from the job's
 * start, so a slow boot does not eat the job's running time. */
async function watch(): Promise<Outcome> {
  const begin = Date.now()
  let startedAt: number | undefined
  let polls = 0
  let last: Job | undefined
  for (;;) {
    polls += 1
    last = await readJob()
    const elapsedS = (Date.now() - begin) / 1000
    if (last?.status === "completed") return { kind: "finished", job: last }
    if (last?.status === "in_progress" && startedAt === undefined) startedAt = Date.now()
    if (startedAt === undefined && elapsedS > startDeadlineS) {
      return last === undefined
        ? { kind: "never-queued", elapsedS, polls }
        : { kind: "never-started", elapsedS, polls, job: last }
    }
    if (startedAt !== undefined && last && (Date.now() - startedAt) / 1000 > finishDeadlineS) {
      return { kind: "never-finished", elapsedS, polls, job: last }
    }
    await Bun.sleep(POLL_MS)
  }
}

function span(from: string | null, to: string | null): number | undefined {
  if (!from || !to) return undefined
  return (Date.parse(to) - Date.parse(from)) / 1000
}

/** One table cell: a step name is workflow text and may hold a `|`. */
function cell(value: unknown): string {
  return clean(value).replaceAll("|", "\\|")
}

function fmt(value: number | undefined): string {
  return value === undefined ? "-" : `${value.toFixed(0)} s`
}

async function cancelRun(): Promise<void> {
  const response = await github("POST", `/repos/${repo}/actions/runs/${runId}/cancel`)
  if (!response.ok) logError(`cancelling run ${runId}: HTTP ${response.status}`)
}

const outcome = await watch()
if (outcome.kind !== "finished") {
  const detail =
    outcome.kind === "never-queued"
      ? `job '${jobName}' never appeared in run ${runId}`
      : `job '${jobName}' is still ${clean(outcome.job.status)}` +
        (outcome.job.runner_name ? ` on runner ${clean(outcome.job.runner_name)}` : ", no runner took it")
  logError(`${outcome.kind} after ${outcome.elapsedS.toFixed(0)} s and ${outcome.polls} polls: ${detail}`)
  await cancelRun()
  process.exit(1)
}

const { job } = outcome
// GitHub stamps created_at when the job is queued and started_at when a
// runner takes it; "Set up job" is the first step, so its start is when the
// job's own code begins to run.
const queueToStart = span(job.created_at, job.started_at)
const firstStep = job.steps?.[0]
const queueToFirstStep = span(job.created_at, firstStep?.started_at ?? null)
const running = span(job.started_at, job.completed_at)
const rows = [
  "| metric | value |",
  "| --- | --- |",
  `| conclusion | ${cell(job.conclusion)} |`,
  `| runner | ${cell(job.runner_name ?? "-")} |`,
  `| queued to runner assigned | ${fmt(queueToStart)} |`,
  `| queued to first step | ${fmt(queueToFirstStep)} |`,
  `| job running time | ${fmt(running)} |`,
  ...(job.steps ?? []).map(
    (step) => `| step: ${cell(step.name)} | ${fmt(span(step.started_at, step.completed_at))} (${cell(step.conclusion)}) |`,
  ),
]
const summary = process.env.GITHUB_STEP_SUMMARY
if (summary) await appendFile(summary, ["", "### ix runner e2e", "", ...rows, ""].join("\n"))
console.log(rows.join("\n"))

if (job.conclusion !== "success") {
  logError(`job '${jobName}' concluded ${clean(job.conclusion)}`)
  process.exit(1)
}
if (startBudgetS !== undefined && (queueToStart === undefined || queueToStart > startBudgetS)) {
  logError(`queue-to-start ${fmt(queueToStart)} exceeds the ${startBudgetS} s budget`)
  process.exit(1)
}
