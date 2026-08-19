/** The tick, end to end: guard, observe, decide, execute, report.
 *
 * Everything interesting lives in the modules this wires together; keeping
 * this file boring is the point - the whole run is readable as five lines. */

import { Client } from "@indexable/sdk"
import { loadConfig } from "./config.ts"
import { decide } from "./decide.ts"
import { execute } from "./execute.ts"
import { GitHub } from "./github.ts"
import { observe } from "./observe.ts"
import { logError, logWarning, writeSummary } from "./report.ts"
import { adminCredential } from "./vend.ts"

/** Hosted runners only. This process holds IX_TOKEN and a repo-admin
 * credential, and it manages the very machines a self-hosted runner would
 * be: running it on one hands both secrets to the thing they exist to
 * control. The README says hosted-only; this is what makes it true. */
function requireHostedRunner(): void {
  if (process.env.IX_RUNNERS_ALLOW_NON_HOSTED === "1") return
  if (process.env.RUNNER_ENVIRONMENT !== "github-hosted") {
    logError(
      `refusing to run: RUNNER_ENVIRONMENT is '${process.env.RUNNER_ENVIRONMENT ?? "unset"}',` +
        " not 'github-hosted'. Set runs-on: ubuntu-latest; on GHES/ARC, set" +
        " IX_RUNNERS_ALLOW_NON_HOSTED=1 to accept the risk explicitly.",
    )
    process.exit(1)
  }
}

requireHostedRunner()
if (!process.env.IX_TOKEN) {
  logError("IX_TOKEN is required: the action's ix-token input")
  process.exit(1)
}
const workflow = process.env.GITHUB_TOKEN
if (!workflow) {
  logError("GITHUB_TOKEN is required: the workflow's own token, for reading the job queue")
  process.exit(1)
}

const config = await loadConfig()
const ix = new Client()
const gh = new GitHub(await adminCredential(ix), workflow, config.repo)

const world = await observe(ix, gh, config)

// The rev this tick derived came from the checkout, and only the default
// branch may steer the fleet: a workflow_dispatch from a feature branch
// would roll every holder to a branch rev and let branch state seed the
// templates. Observation is read-only, so refusing here has cost nothing.
const ref = process.env.GITHUB_REF_NAME
if (ref && ref !== world.defaultBranch) {
  logError(
    `refusing to reconcile from ref '${ref}': the runner config is taken from` +
      ` '${world.defaultBranch}' only. Dispatch this workflow from '${world.defaultBranch}'.`,
  )
  process.exit(1)
}

const plan = decide(config, world, Date.now())

for (const note of plan.notes) {
  if (note.level === "warn") logWarning(note.text)
  else console.log(note.text)
}
console.log(
  `pool ${config.pool} @ ${world.rev.slice(0, 8)}: ${world.machines.length} machine(s),` +
    ` ${world.registrations.length} registration(s),` +
    ` ${world.queue === null ? "queue unreadable" : `${world.queue.demanded.length} demanded job(s)`},` +
    ` ${plan.steps.length} step(s)`,
)

const outcome = await execute(ix, gh, plan)
for (const [subject, action, result] of outcome.rows) console.log(`${subject}: ${action} -> ${result}`)
await writeSummary(outcome.rows)
process.exit(outcome.failures > 0 ? 1 : 0)
