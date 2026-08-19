/** Everything that changes the world. Nothing here decides anything.
 *
 * Two properties are load-bearing and easy to lose (both were paid for in
 * v1): one step's failure is that step's failure - logged, budget stays
 * spent, siblings continue - and every ADDITIVE step finishes before any
 * destructive one begins, so a tick that dies halfway leaves the pool
 * larger than intended, never smaller. */

import type { Client } from "@indexable/sdk"
import { NotFound } from "@indexable/sdk"
import type { GitHub } from "./github.ts"
import { clean, logError, mask } from "./report.ts"
import type { Plan, Step } from "./types.ts"

/** Where the guest's path unit watches for its single-job credential. */
const JITCONFIG_PATH = "/var/lib/ix-runner/jitconfig"
/** Snapshot capture of a CI machine's disk, observed under a minute; three
 * minutes is the point past which waiting costs more than a cold boot. */
const SNAPSHOT_WAIT_MS = 180_000

export interface Outcome {
  /** (subject, action, outcome) rows for the job summary. */
  readonly rows: (readonly [string, string, string])[]
  readonly failures: number
}

export async function execute(ix: Client, gh: GitHub, plan: Plan): Promise<Outcome> {
  const outcome: { rows: [string, string, string][]; failures: number } = {
    rows: [],
    failures: 0,
  }
  const additive = plan.steps.filter((step) => step.do === "spawn" || step.do === "promote")
  const destructive = plan.steps.filter((step) => step.do !== "spawn" && step.do !== "promote")

  await Promise.all(additive.map((step) => run(step)))
  // Destructive steps run one at a time: each deregister already paces
  // itself against GitHub's secondary rate limit, and there is never a
  // hurry to destroy anything.
  for (const step of destructive) await run(step)
  return outcome

  async function run(step: Step): Promise<void> {
    const subject =
      step.do === "promote" ? step.winner.name : step.do === "spawn" ? step.name : step.do === "deregister" ? step.name : step.machine.name
    try {
      outcome.rows.push([subject, step.do, await apply(step)])
    } catch (error) {
      outcome.failures += 1
      logError(`${subject}: ${step.do} FAILED (${clean(error)}); reconciling again next tick`)
      outcome.rows.push([subject, step.do, `FAILED: ${clean(error)}`])
    }
  }

  async function apply(step: Step): Promise<string> {
    switch (step.do) {
      case "spawn":
        return await spawn(step)
      case "promote":
        return await promote(step)
      case "retire": {
        if (!(await gh.deregister(step.registrationIds))) return "skipped (busy)"
        await destroy(step.machine.id)
        return `retired (${step.why})`
      }
      case "delete": {
        await destroy(step.machine.id)
        return `deleted (${step.why})`
      }
      case "stop": {
        await ix.machines().connect(step.machine.id).stop()
        return `stopped (${step.why})`
      }
      case "deregister": {
        if (!(await gh.deregister(step.registrationIds))) return "skipped (busy)"
        return `deregistered (${step.why})`
      }
    }
  }

  async function spawn(step: Step & { do: "spawn" }): Promise<string> {
    const machine = await ix.machines().create({
      name: step.name,
      region: step.region,
      ...step.source,
    })
    // The machine exists before its credential does: a failure past this
    // point deletes it, or a registered-but-machineless runner would be
    // GitHub's view of this pool forever.
    try {
      const jit = await gh.mintJitConfig(step.name, step.labels)
      // Masked BEFORE it can reach any other output: for its lifetime this
      // blob can register a runner that steals the repository's jobs.
      mask(jit)
      await machine.writeFile(JITCONFIG_PATH, jit)
    } catch (error) {
      try {
        await destroy(machine.id())
      } catch (cleanup) {
        // The root cause is `error`; a failed cleanup only adds a warning.
        logError(`${step.name}: deleting the half-spawned machine also failed (${clean(cleanup)})`)
      }
      throw error
    }
    return "snapshot" in step.source ? "spawned from seed" : "spawned cold"
  }

  /** Snapshot the winner, swap it into the holder name, stop it. Ordered so
   * a crash at any line leaves either no change or a retiring holder beside
   * a fresh one - states the next tick's decider already knows how to
   * finish. The old holder is never deleted here: that happens next tick,
   * once this machine's snapshot is listed as ready. */
  async function promote(step: Step & { do: "promote" }): Promise<string> {
    const winner = ix.machines().connect(step.winner.id)
    const ref = await winner.snapshot()
    const wait = await winner.waitSnapshotReady(ref.snapshotId, SNAPSHOT_WAIT_MS)
    if (wait !== "ready") throw new Error(`snapshot ${ref.snapshotId} ended ${wait}`)
    if (step.oldHolder !== undefined) {
      // A -retiring machine can already exist if the previous promotion's
      // cleanup has not run yet; it is strictly staler than what it would
      // be replaced by, so it goes first.
      await destroyByName(`${step.oldHolder.name}-retiring`)
      await ix.machines().connect(step.oldHolder.id).rename(`${step.oldHolder.name}-retiring`)
    }
    await winner.rename(step.holderName)
    await winner.stop()
    return `now holds the ${step.holderName.split("-").at(-2)} seed`
  }

  async function destroy(machineId: string): Promise<void> {
    try {
      await ix.machines().connect(machineId).delete()
    } catch (error) {
      if (error instanceof NotFound) return // already gone is the goal state
      throw error
    }
  }

  async function destroyByName(name: string): Promise<void> {
    let id: string
    try {
      id = (await ix.machines().get(name)).id
    } catch (error) {
      if (error instanceof NotFound) return
      throw error
    }
    await destroy(id) // the delete itself can NotFound-race a concurrent tick
  }
}
