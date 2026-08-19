/** The whole policy, as one pure function. No I/O, no SDK, no clock reads:
 * a World and a timestamp go in, a Plan comes out, and every rule in the
 * README is a line in here that a test can pin.
 *
 * Ordering inside the plan is meaningless - the executor phases additive
 * steps before destructive ones - but every step must be safe against the
 * others, because any of them can fail independently. */

import type { Config } from "./config.ts"
import { lineageKey, parseRole, runnerName, seedName } from "./names.ts"
import type { Labels, MachineRow, Plan, Registration, Step, World } from "./types.ts"

/** Slack for LEGACY holders only: seeds promoted before the holder name
 * carried its source job's completion time.
 *
 * A current holder's name encodes its evidence, so the freshness gate
 * compares job time to job time exactly. A legacy holder only has
 * `snapshotAt` - CAPTURE time, which postdates its source job by the
 * promoting tick's scan-to-capture latency (seconds, at most a minute or
 * two) - so the gate biases by this slack against it. The slack cuts both
 * ways and is deliberately SMALL: too large and an older leftover runner
 * can REGRESS a legacy seed (a wide slack admits evidence older than the
 * seed's own); too small and a genuinely newer run reads as stale. Legacy
 * holders also refuse any winner whose evidence predates the holder
 * machine's creation - a machine cannot hold evidence from before it
 * existed. A rev roll deletes every holder, so legacy names wash out. */
export const LEGACY_CAPTURE_SKEW_MS = 5 * 60 * 1000

export function decide(config: Config, world: World, nowMs: number): Plan {
  const steps: Step[] = []
  const notes: { level: "info" | "warn"; text: string }[] = []
  const rev8 = world.rev.slice(0, 8)

  const role = (machine: MachineRow) => parseRole(config.pool, machine.name)
  const registrationsByName = new Map<string, Registration[]>()
  for (const registration of world.registrations) {
    const list = registrationsByName.get(registration.name) ?? []
    list.push(registration)
    registrationsByName.set(registration.name, list)
  }

  // -- Failed machines serve nothing on any tick: clear them, registered
  // or not. A failed holder loses its lineage's seed; the next green
  // default-branch run re-establishes it, and until then spawns boot cold.
  // This deliberately includes a promotion WINNER whose machine flips to
  // failed mid-capture: its snapshot is doomed with the machine (a capture
  // cannot outlive its source), so sweeping it is not a lost seed - it is
  // the only honest outcome, and the next green run promotes instead.
  const failed = world.machines.filter((machine) => machine.status === "failed")
  for (const machine of failed) {
    const registrations = registrationsByName.get(machine.name) ?? []
    const why = `failed: ${machine.failureReason ?? "no reason recorded"}`
    if (registrations.length > 0) {
      steps.push({
        do: "retire",
        machine,
        registrationIds: registrations.map((registration) => registration.id),
        why,
      })
    } else {
      steps.push({ do: "delete", machine, why })
    }
  }

  const alive = world.machines.filter((machine) => machine.status !== "failed")
  const runners = alive.filter((machine) => role(machine)?.kind === "runner")
  const holders = alive.filter((machine) => role(machine)?.kind === "seed")

  const currentHolders = new Map<string, MachineRow>() // lineage -> holder
  const seedEvidence = (machine: MachineRow): number | undefined => {
    const parsed = role(machine)
    return parsed?.kind === "seed" ? parsed.evidenceAtSec : undefined
  }
  for (const holder of holders) {
    const parsed = role(holder)
    if (parsed?.kind !== "seed" || parsed.rev !== rev8 || parsed.retiring) continue
    // Evidence-suffixed names mean two non-retiring holders CAN coexist
    // briefly around a crash; the one with the newest evidence is the
    // lineage's seed (a legacy name with unknown evidence counts oldest).
    const incumbent = currentHolders.get(parsed.lineage)
    if (incumbent !== undefined) {
      const standing = seedEvidence(incumbent)
      if (standing !== undefined && (parsed.evidenceAtSec ?? -Infinity) <= standing) continue
    }
    currentHolders.set(parsed.lineage, holder)
  }

  // A failed machine still owns its NAME. Promotion renames the winner into
  // the holder name, so a failed incumbent must be handed over as the
  // machine to move aside - its id-keyed delete above still lands - or the
  // rename collides every tick until the failed sweep wins the race.
  const failedHolderByName = new Map(
    failed
      .filter((machine) => role(machine)?.kind === "seed")
      .map((machine) => [machine.name, machine]),
  )

  // -- Holder GC is decided from names and snapshot listings alone (all
  // ix-side facts), so it runs even when GitHub was unreadable.
  for (const holder of holders) {
    const parsed = role(holder)
    if (parsed?.kind !== "seed") continue
    if (parsed.rev !== rev8) {
      // The runner config rolled: this lineage restarts from the new
      // template, and a seed descended from the old config must not leak
      // into it. Deleting the holder deletes its snapshot with it.
      steps.push({ do: "delete", machine: holder, why: `rev rolled to ${rev8}` })
      continue
    }
    if (!parsed.retiring && holder.status === "running") {
      // A promotion that died between rename and stop leaves the holder
      // running: it serves nothing and bills until the lineage's next
      // promotion, which for a quiet lineage is never.
      steps.push({ do: "stop", machine: holder, why: "holder left running" })
    }
    if (parsed.retiring) {
      const successor = currentHolders.get(parsed.lineage)
      const successorSeed = successor && world.seeds.get(successor.id)
      if (successorSeed?.snapshotId !== undefined) {
        steps.push({ do: "delete", machine: holder, why: "promotion completed" })
      } else {
        // Mid-promotion crash: the successor is absent or its snapshot is
        // not restorable yet. This retiring holder is the lineage's last
        // good seed - keeping it is the recovery. The hold is deliberately
        // UNBOUNDED: while the snapshot pipeline is sick a retiring holder
        // can sit (and bill) for days, warning every tick, but deleting it
        // on any timer would trade a storage bill for losing the lineage's
        // only restorable seed. Intentional, not a leak.
        notes.push({
          level: "warn",
          text: `${holder.name}: keeping the retiring holder, its successor has no ready snapshot yet`,
        })
      }
    }
  }

  if (world.queue === null) {
    // Missing data is not zero demand. Nothing that reads the queue -
    // spawning, promoting, retiring, even deleting a finished runner -
    // may act on a tick that could not see it.
    notes.push({ level: "warn", text: "queue unreadable: converging nothing this tick" })
    return { steps, notes }
  }
  const queue = world.queue
  if (queue.truncated) {
    notes.push({
      level: "info",
      text: "more active runs than the scan window reads: demand is a floor this tick",
    })
  }

  // -- Promotion: the newest successful default-branch job per lineage
  // whose runner machine is still here becomes the lineage's seed. The
  // finished window re-reads the same evidence every tick; what makes that
  // idempotent is that promotion RENAMES the winner out of the runner
  // namespace, so surviving evidence stops matching any machine.
  const runnerByName = new Map(runners.map((machine) => [machine.name, machine]))
  const promoted = new Set<string>() // machine ids leaving the runner pool
  const winners = new Map<string, { machine: MachineRow; completedAt: number }>()
  const notedOffBranch = new Set<string>()
  for (const job of queue.finished) {
    if (!job.succeeded) continue
    const machine = runnerByName.get(job.runnerName)
    if (machine === undefined) continue
    if ((registrationsByName.get(machine.name) ?? []).length > 0) continue // still settling
    const parsed = role(machine)
    if (parsed?.kind !== "runner") continue
    if (!job.onDefaultBranch) {
      // A green machine that can never seed. Said out loud, or its
      // job-finished deletion below is indistinguishable in the log from a
      // promotable winner's - the shape of every "why did the seed not
      // refresh?" hunt.
      if (!notedOffBranch.has(machine.name)) {
        notedOffBranch.add(machine.name)
        notes.push({
          level: "info",
          text: `${machine.name}: green job did not run on the default branch; not a seed candidate`,
        })
      }
      continue
    }
    const best = winners.get(parsed.lineage)
    if (best === undefined || job.completedAt > best.completedAt) {
      winners.set(parsed.lineage, { machine, completedAt: job.completedAt })
    }
  }
  for (const [lineage, winner] of winners) {
    const oldHolder = currentHolders.get(lineage)
    const oldEvidenceSec = oldHolder && seedEvidence(oldHolder)
    const oldSeed = oldHolder && world.seeds.get(oldHolder.id)
    // A skipped promotion is never silent: the winner falls through to
    // job-finished deletion below, and a skip must be distinguishable from
    // plain cleanup - the shape of every "why did the seed not refresh?" hunt.
    if (oldEvidenceSec !== undefined) {
      // The holder's name carries its source job's completion: job time
      // against job time, exactly, no slack in either direction.
      if (oldEvidenceSec >= winner.completedAt) {
        notes.push({
          level: "info",
          text:
            `${lineage}: not promoting ${winner.machine.name} - the seed already holds` +
            ` evidence from ${new Date(oldEvidenceSec * 1000).toISOString()}, no older than` +
            ` this job (completed ${new Date(winner.completedAt * 1000).toISOString()})`,
        })
        continue
      }
    } else if (oldHolder !== undefined && oldSeed?.snapshotAt !== undefined) {
      // Legacy holder: its name predates the evidence suffix, so only its
      // capture time is known. Two guards, both refusing the winner: the
      // snapshot predates the evidence beyond the capture skew, or the
      // evidence predates the holder machine itself (a machine cannot hold
      // evidence from before it existed).
      if (oldSeed.snapshotAt - LEGACY_CAPTURE_SKEW_MS >= winner.completedAt * 1000) {
        notes.push({
          level: "info",
          text:
            `${lineage}: not promoting ${winner.machine.name} - the legacy seed's snapshot` +
            ` (${new Date(oldSeed.snapshotAt).toISOString()}) already postdates its evidence` +
            ` (job completed ${new Date(winner.completedAt * 1000).toISOString()})`,
        })
        continue
      }
      if (winner.completedAt * 1000 <= oldHolder.createdAt) {
        notes.push({
          level: "info",
          text:
            `${lineage}: not promoting ${winner.machine.name} - its job completed` +
            ` (${new Date(winner.completedAt * 1000).toISOString()}) before the legacy holder` +
            ` machine even existed (${new Date(oldHolder.createdAt).toISOString()})`,
        })
        continue
      }
    }
    const holderName = seedName(config.pool, lineage, world.rev, winner.completedAt)
    steps.push({
      do: "promote",
      winner: winner.machine,
      holderName,
      oldHolder: oldHolder ?? failedHolderByName.get(holderName),
    })
    promoted.add(winner.machine.id)
  }

  // -- Finished runners: a runner machine with no registration is done
  // (JIT runners deregister themselves after their one job) - unless it
  // was just spawned and its registration has not appeared yet, which is
  // what the warm grace absorbs.
  //
  // "Done" requires EVIDENCE, not inference: deregistration is visible
  // seconds after a job ends, while the finished-job observation can lag
  // it. A machine deleted on deregistration alone destroys the disk state
  // promotion exists to capture - the seed race that ate every green
  // canary machine of a long multi-job run. So a job-finished delete
  // demands the machine's completed job in this tick's queue observation
  // (the same evidence promotion reads); a machine whose evidence never
  // arrives (cancelled run, evicted scan window) falls to the idle-grace
  // backstop below instead.
  const finishedEvidence = new Set(queue.finished.map((job) => job.runnerName))
  const deleted = new Set<string>()
  for (const machine of runners) {
    if (promoted.has(machine.id)) continue
    if ((registrationsByName.get(machine.name) ?? []).length > 0) continue
    if (nowMs - machine.createdAt < config.warmGraceSeconds * 1000) continue
    if (finishedEvidence.has(machine.name)) {
      steps.push({ do: "delete", machine, why: "job finished" })
      deleted.add(machine.id)
    } else if (nowMs - machine.createdAt >= config.idleGraceSeconds * 1000) {
      // Evidence loss must never be silent: if this machine's job was green
      // on the default branch, deleting it here is the seed candidacy being
      // destroyed - say so, loudly, so an evicted scan window is findable.
      notes.push({
        level: "warn",
        text:
          `${machine.name}: deleting past the idle grace with NO finished-job` +
          " evidence in the scan window. If its job was green on the default" +
          " branch, its seed candidacy is lost with it.",
      })
      steps.push({ do: "delete", machine, why: "no registration past idle grace" })
      deleted.add(machine.id)
    }
    // else: deregistered but unevidenced and young - hold for the evidence
    // (or the backstop) on a later tick.
  }

  // -- Dark runners: registered (the registration is minted alongside the
  // machine) but every registration offline past the idle grace - the guest
  // never came online, or died without finishing its job. No other rule
  // touches this state, and it would count as standing capacity forever.
  // An offline runner cannot be holding a job, and one that comes online
  // mid-flight turns the deregister into GitHub's 422 refusal: race-free.
  for (const machine of runners) {
    if (promoted.has(machine.id) || deleted.has(machine.id)) continue
    const registrations = registrationsByName.get(machine.name) ?? []
    if (registrations.length === 0) continue
    if (registrations.some((r) => r.online || r.busy)) continue
    if (nowMs - machine.createdAt < config.idleGraceSeconds * 1000) continue
    steps.push({
      do: "retire",
      machine,
      registrationIds: registrations.map((registration) => registration.id),
      why: "runner never came online (or went dark)",
    })
    deleted.add(machine.id)
  }

  // -- Registrations with no machine behind them: a spawn that died between
  // JIT mint and machine readiness leaves one. Offline and idle only -
  // anything online answers for a machine this listing failed to show.
  // failed included: their retire step above already carries the deregister
  const machineNames = new Set(world.machines.map((machine) => machine.name))
  for (const [name, registrations] of registrationsByName) {
    if (machineNames.has(name)) continue
    const orphans = registrations.filter(
      (registration) => !registration.online && !registration.busy,
    )
    if (orphans.length === 0) continue
    steps.push({
      do: "deregister",
      name,
      registrationIds: orphans.map((registration) => registration.id),
      why: "registration without a machine",
    })
  }

  // -- Demand. A lineage's labels are only learnable from jobs (the name
  // holds a hash), so min-warm reaches exactly the lineages that appear in
  // the demand or finished windows; a lineage idle past both windows warms
  // back up on its first job instead.
  const labelsByLineage = new Map<string, Labels>()
  const demand = new Map<string, number>()
  for (const job of queue.demanded) {
    const lineage = lineageKey(job.labels)
    labelsByLineage.set(lineage, job.labels)
    demand.set(lineage, (demand.get(lineage) ?? 0) + 1)
  }
  if (config.minWarm > 0) {
    for (const job of queue.finished) {
      if (!job.labels.includes(config.runnerLabel)) continue
      const lineage = lineageKey(job.labels)
      if (currentHolders.has(lineage) && !labelsByLineage.has(lineage)) {
        labelsByLineage.set(lineage, job.labels)
        demand.set(lineage, demand.get(lineage) ?? 0)
      }
    }
  }

  const serving = (lineage: string) =>
    runners.filter((machine) => {
      const parsed = role(machine)
      return (
        parsed?.kind === "runner" &&
        parsed.lineage === lineage &&
        !promoted.has(machine.id) &&
        !deleted.has(machine.id)
      )
    })

  let budget = Math.max(0, config.maxRunners - runners.length + promoted.size + deleted.size)
  let coldBudget = config.maxColdBoots
  const wants = new Map<string, number>()
  for (const [lineage, queued] of [...demand.entries()].sort()) {
    const labels = labelsByLineage.get(lineage)!
    const want = queued > 0 ? queued + config.headroom : config.minWarm
    wants.set(lineage, want)
    const have = serving(lineage).length
    const holder = currentHolders.get(lineage)
    const seed = holder && world.seeds.get(holder.id)
    const region = config.regions[parseInt(lineage, 16) % config.regions.length]!
    for (let i = have; i < want; i++) {
      if (budget === 0) {
        notes.push({
          level: "warn",
          text: `${lineage}: demand exceeds max-runners (${config.maxRunners}); leaving the rest queued`,
        })
        break
      }
      let source: { snapshot: string } | { template: string }
      if (seed?.snapshotId !== undefined) {
        source = { snapshot: seed.snapshotId }
      } else {
        if (coldBudget === 0) {
          notes.push({
            level: "warn",
            text: `${lineage}: cold-boot budget spent (max-cold-boots ${config.maxColdBoots}); retrying next tick`,
          })
          break
        }
        coldBudget -= 1
        source = { template: templateRef(config, world.rev) }
      }
      budget -= 1
      steps.push({
        do: "spawn",
        name: runnerName(config.pool, lineage),
        labels,
        source,
        region,
      })
    }
  }

  // -- Retirement of surplus standbys: registered, idle, past the grace,
  // and only when this tick is allowed to shrink anything at all. A JIT
  // standby has never run a job (it would be gone if it had), so its
  // creation time IS its idle-since.
  if (config.mayScaleDown) {
    const retiring = new Set<string>()
    for (const machine of runners) {
      if (promoted.has(machine.id) || deleted.has(machine.id)) continue
      const parsed = role(machine)
      if (parsed?.kind !== "runner") continue
      const registrations = registrationsByName.get(machine.name) ?? []
      const idle = registrations.length > 0 && registrations.every((r) => r.online && !r.busy)
      if (!idle) continue
      if (nowMs - machine.createdAt < config.idleGraceSeconds * 1000) continue
      const want = wants.get(parsed.lineage) ?? 0
      const standing = serving(parsed.lineage).filter((m) => !retiring.has(m.id)).length
      if (standing <= want) continue
      retiring.add(machine.id)
      steps.push({
        do: "retire",
        machine,
        registrationIds: registrations.map((registration) => registration.id),
        why: `idle past ${config.idleGraceSeconds}s with no demand`,
      })
    }
  }

  return { steps, notes }
}

/** The sha-pinned flake reference a cold boot builds. Pinned by rev so the
 * platform's template cache is keyed by exactly the config that produced it.
 * templateRepo, not repo: in pool mode the template lives in the ACTION's
 * repository, while every GitHub call stays on the customer's. */
function templateRef(config: Config, rev: string): string {
  const dir = config.flakeDir ? `?dir=${config.flakeDir}` : ""
  return `github:${config.templateRepo}/${rev}${dir}#${config.templateAttr}`
}
