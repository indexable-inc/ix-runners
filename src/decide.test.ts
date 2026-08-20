/** The policy, pinned. decide() is pure, so every rule the README states is
 * one World literal and one assertion here. */

import { describe, expect, test } from "bun:test"
import type { Config } from "./config.ts"
import { decide } from "./decide.ts"
import { lineageKey, seedName } from "./names.ts"
import type {
  FinishedJob,
  MachineRow,
  Registration,
  Seed,
  Step,
  World,
} from "./types.ts"

const REV = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
const NOW = 1_800_000_000_000
const LABELS = ["16vcpu", "ix"] as const
const LINEAGE = lineageKey([...LABELS])
const HOLDER = seedName("p", LINEAGE, REV)

const config: Config = {
  repo: "acme/app",
  pool: "p",
  templateAttr: "ci-runner",
  flakeDir: "",
  regions: ["us-west-1"],
  runnerLabel: "ix",
  maxRunners: 16,
  headroom: 1,
  minWarm: 0,
  idleGraceSeconds: 900,
  maxColdBoots: 4,
  warmGraceSeconds: 300,
  mayScaleDown: true,
  templateRepo: "acme/app",
  templateRev: "",
}

let nextId = 0
function machine(name: string, over: Partial<MachineRow> = {}): MachineRow {
  return { id: `m${nextId++}`, name, status: "running", createdAt: NOW - 3_600_000, ...over }
}
function registration(name: string, over: Partial<Registration> = {}): Registration {
  return { id: nextId++, name, online: true, busy: false, ...over }
}
function finished(runnerName: string, over: Partial<FinishedJob> = {}): FinishedJob {
  return {
    runnerName,
    labels: [...LABELS],
    completedAt: NOW / 1000 - 60,
    succeeded: true,
    onDefaultBranch: true,
    ...over,
  }
}
function world(over: Partial<World> = {}): World {
  return {
    rev: REV,
    defaultBranch: "main",
    machines: [],
    registrations: [],
    queue: { demanded: [], finished: [], truncated: false },
    seeds: new Map(),
    ...over,
  }
}
function steps(w: World, c: Config = config): Step[] {
  return [...decide(c, w, NOW).steps]
}
function only<T extends Step["do"]>(list: Step[], kind: T): (Step & { do: T })[] {
  return list.filter((step): step is Step & { do: T } => step.do === kind)
}

describe("spawning", () => {
  test("a queued job with no machines spawns one plus headroom, cold", () => {
    const plan = steps(world({ queue: { demanded: [{ labels: [...LABELS] }], finished: [], truncated: false } }))
    const spawns = only(plan, "spawn")
    expect(spawns).toHaveLength(2) // 1 queued + 1 headroom
    for (const spawn of spawns) {
      expect(spawn.labels).toEqual([...LABELS])
      expect(spawn.source).toEqual({ template: `github:acme/app/${REV}#ci-runner` })
      expect(spawn.region).toBe("us-west-1")
      expect(spawn.name).toMatch(new RegExp(`^p-run-${LINEAGE}-`))
    }
  })

  test("pool mode cold-boots from the action's own repo at the action rev", () => {
    // `pool:` mode: the template lives in the ACTION's repository, pinned
    // at the action's own commit (which is also what world.rev carries,
    // via resolveRev). The exact string is the seam: everything GitHub-side
    // stays keyed on config.repo, only the flake ref moves.
    const actionRev = "4751cbbab884173b3a3bcee7c19808e89a18bb37"
    const pinned: Config = {
      ...config,
      flakeDir: "pools/baml",
      templateRepo: "indexable-inc/ix-runners",
      templateRev: actionRev,
    }
    const plan = steps(
      world({
        rev: actionRev,
        queue: { demanded: [{ labels: [...LABELS] }], finished: [], truncated: false },
      }),
      pinned,
    )
    for (const spawn of only(plan, "spawn")) {
      expect(spawn.source).toEqual({
        template: `github:indexable-inc/ix-runners/${actionRev}?dir=pools/baml#ci-runner`,
      })
    }
    expect(only(plan, "spawn").length).toBeGreaterThan(0)
  })

  test("a ready seed makes spawns restores, not cold boots", () => {
    const holder = machine(HOLDER, { status: "stopped" })
    const seed: Seed = { holder, snapshotId: "snap1", snapshotAt: NOW - 60_000 }
    const plan = steps(
      world({
        machines: [holder],
        queue: { demanded: [{ labels: [...LABELS] }], finished: [], truncated: false },
        seeds: new Map([[holder.id, seed]]),
      }),
    )
    for (const spawn of only(plan, "spawn")) expect(spawn.source).toEqual({ snapshot: "snap1" })
  })

  test("a seed-sourced spawn names its holder, so the executor can retire a dead seed", () => {
    const holder = machine(HOLDER, { status: "stopped" })
    const plan = steps(
      world({
        machines: [holder],
        queue: { demanded: [{ labels: [...LABELS] }], finished: [], truncated: false },
        seeds: new Map([[holder.id, { holder, snapshotId: "snap1", snapshotAt: NOW - 60_000 }]]),
      }),
    )
    const spawns = only(plan, "spawn")
    expect(spawns.length).toBeGreaterThan(0)
    for (const spawn of spawns) expect(spawn.seedHolder?.id).toBe(holder.id)
  })

  test("a lineage whose only holder is retiring spawns cold, never from its snapshot", () => {
    // The dead-seed escape hatch's second half: the executor renamed a
    // refused seed's holder to -retiring, so the next (stateless) tick
    // must source spawns from the template, not the dead snapshot -
    // even though the retiring holder still lists a ready snapshot.
    const retiring = machine(`${HOLDER}-retiring`, { status: "stopped" })
    const plan = steps(
      world({
        machines: [retiring],
        queue: { demanded: [{ labels: [...LABELS] }], finished: [], truncated: false },
        seeds: new Map([[retiring.id, { holder: retiring, snapshotId: "dead", snapshotAt: NOW }]]),
      }),
    )
    const spawns = only(plan, "spawn")
    expect(spawns.length).toBeGreaterThan(0)
    for (const spawn of spawns) {
      expect(spawn.source).toEqual({ template: `github:acme/app/${REV}#ci-runner` })
      expect(spawn.seedHolder).toBeUndefined()
    }
  })

  test("existing runners count against want", () => {
    const standing = machine(`p-run-${LINEAGE}-x1`)
    const plan = steps(
      world({
        machines: [standing],
        registrations: [registration(standing.name)],
        queue: { demanded: [{ labels: [...LABELS] }], finished: [], truncated: false },
      }),
    )
    expect(only(plan, "spawn")).toHaveLength(1) // want 2, have 1
  })

  test("max-runners caps admission (seeded, so the cold budget is idle)", () => {
    const holder = machine(HOLDER, { status: "stopped" })
    const demanded = Array.from({ length: 30 }, () => ({ labels: [...LABELS] }))
    const plan = decide(
      config,
      world({
        machines: [holder],
        seeds: new Map([[holder.id, { holder, snapshotId: "s", snapshotAt: NOW }]]),
        queue: { demanded, finished: [], truncated: false },
      }),
      NOW,
    )
    expect(only([...plan.steps], "spawn").length).toBe(config.maxRunners)
    expect(plan.notes.some((n) => n.text.includes("max-runners"))).toBe(true)
  })

  test("cold boots are budgeted separately", () => {
    const demanded = Array.from({ length: 10 }, () => ({ labels: [...LABELS] }))
    const plan = decide(config, world({ queue: { demanded, finished: [], truncated: false } }), NOW)
    expect(only([...plan.steps], "spawn").length).toBe(config.maxColdBoots)
    expect(plan.notes.some((n) => n.text.includes("cold-boot budget"))).toBe(true)
  })
})

describe("the unreadable queue", () => {
  test("makes no capacity decision at all", () => {
    const idle = machine(`p-run-${LINEAGE}-x1`, { createdAt: NOW - 10_000_000 })
    const done = machine(`p-run-${LINEAGE}-x2`) // unregistered: normally deleted
    const plan = decide(
      config,
      world({ machines: [idle, done], registrations: [registration(idle.name)], queue: null }),
      NOW,
    )
    expect(plan.steps).toHaveLength(0)
    expect(plan.notes.some((n) => n.level === "warn" && n.text.includes("unreadable"))).toBe(true)
  })

  test("still clears failed machines and rolls stale holders", () => {
    const dead = machine(`p-run-${LINEAGE}-x1`, { status: "failed", failureReason: "host died" })
    const stale = machine(`p-seed-${LINEAGE}-bbbbbbbb`, { status: "stopped" })
    const plan = steps(world({ machines: [dead, stale], queue: null }))
    expect(plan).toHaveLength(2)
    expect(only(plan, "delete").map((s) => s.machine.name).sort()).toEqual(
      [dead.name, stale.name].sort(),
    )
  })
})

describe("promotion", () => {
  test("the newest green default-branch runner becomes the seed", () => {
    const older = machine(`p-run-${LINEAGE}-x1`)
    const newer = machine(`p-run-${LINEAGE}-x2`)
    const plan = steps(
      world({
        machines: [older, newer],
        queue: {
          demanded: [],
          finished: [
            finished(older.name, { completedAt: NOW / 1000 - 600 }),
            finished(newer.name, { completedAt: NOW / 1000 - 60 }),
          ],
          truncated: false,
        },
      }),
    )
    const promotes = only(plan, "promote")
    expect(promotes).toHaveLength(1)
    expect(promotes[0]!.winner.name).toBe(newer.name)
    // The holder name carries the winner's source-job completion time.
    expect(promotes[0]!.holderName).toBe(seedName("p", LINEAGE, REV, NOW / 1000 - 60))
    // the loser is a finished runner, deleted
    expect(only(plan, "delete").map((s) => s.machine.name)).toEqual([older.name])
  })

  test("PR jobs and failures never promote", () => {
    const pr = machine(`p-run-${LINEAGE}-x1`)
    const red = machine(`p-run-${LINEAGE}-x2`)
    const plan = decide(
      config,
      world({
        machines: [pr, red],
        queue: {
          demanded: [],
          finished: [
            finished(pr.name, { onDefaultBranch: false }),
            finished(red.name, { succeeded: false }),
          ],
          truncated: false,
        },
      }),
      NOW,
    )
    expect(only([...plan.steps], "promote")).toHaveLength(0)
    expect(only([...plan.steps], "delete")).toHaveLength(2) // both are just finished runners
    // A green-but-off-branch machine says why it is not a seed candidate.
    expect(
      plan.notes.filter((n) => n.text.includes(`${pr.name}: green job did not run`)),
    ).toHaveLength(1)
  })

  test("a still-registered winner waits for its runner to settle", () => {
    const winner = machine(`p-run-${LINEAGE}-x1`)
    const plan = steps(
      world({
        machines: [winner],
        registrations: [registration(winner.name)],
        queue: { demanded: [], finished: [finished(winner.name)], truncated: false },
      }),
    )
    expect(only(plan, "promote")).toHaveLength(0)
    expect(only(plan, "delete")).toHaveLength(0)
  })

  test("evidence older than the current seed does not re-promote", () => {
    // Older beyond MAX_CAPTURE_SKEW_MS: snapshotAt is capture time, so only
    // a gap the skew cannot explain proves the leftover's evidence is stale.
    const holder = machine(HOLDER, { status: "stopped" })
    const leftover = machine(`p-run-${LINEAGE}-x1`)
    const plan = decide(
      config,
      world({
        machines: [holder, leftover],
        seeds: new Map([[holder.id, { holder, snapshotId: "s", snapshotAt: NOW - 1000 }]]),
        queue: {
          demanded: [],
          finished: [finished(leftover.name, { completedAt: (NOW - 2_000_000) / 1000 })],
          truncated: false,
        },
      }),
      NOW,
    )
    expect(only([...plan.steps], "promote")).toHaveLength(0)
    expect(only([...plan.steps], "delete").map((s) => s.machine.name)).toEqual([leftover.name])
    // The skip is never silent: it must be distinguishable from cleanup.
    expect(plan.notes.some((n) => n.text.includes(`not promoting ${leftover.name}`))).toBe(true)
  })

  test("two-tick seed regression: an older survivor cannot overwrite a fresher seed", () => {
    // The review's blocker scenario, end to end. Tick 1: same-lineage green
    // machines A (older job, still registered so "still settling") and B
    // (newer job, settled) - B promotes, A survives untouched. Tick 2: A
    // has deregistered; its evidence is only minutes older than the seed's,
    // well inside any snapshot-time slack. Under a slack-based gate A would
    // regress the seed to older state; the holder NAME carrying B's job
    // time is what refuses it. The times are chosen inside every slack ever
    // used (old 20m, legacy 5m) so only job-time-vs-job-time can pass.
    const tA = NOW / 1000 - 180 // A's job: 3 minutes ago
    const tB = NOW / 1000 - 120 // B's job: 2 minutes ago, the fresher evidence
    const a = machine(`p-run-${LINEAGE}-aaa1`)
    const b = machine(`p-run-${LINEAGE}-bbb1`)
    const tick1 = steps(
      world({
        machines: [a, b],
        registrations: [registration(a.name)], // A still settling
        queue: {
          demanded: [],
          finished: [
            finished(a.name, { completedAt: tA }),
            finished(b.name, { completedAt: tB }),
          ],
          truncated: false,
        },
      }),
    )
    expect(only(tick1, "promote").map((s) => s.winner.name)).toEqual([b.name])
    expect(only(tick1, "delete")).toHaveLength(0) // A survives tick 1
    const holderName = only(tick1, "promote")[0]!.holderName

    // Tick 2: B's promotion landed - the holder wears the name tick 1
    // minted - and A deregistered. A must be cleaned up, never promoted.
    const holder = machine(holderName, { status: "stopped" })
    const tick2 = steps(
      world({
        machines: [holder, a],
        seeds: new Map([
          [holder.id, { holder, snapshotId: "s", snapshotAt: NOW - 30_000 }],
        ]),
        queue: {
          demanded: [],
          finished: [finished(a.name, { completedAt: tA })],
          truncated: false,
        },
      }),
    )
    expect(only(tick2, "promote")).toHaveLength(0)
    expect(only(tick2, "delete").map((s) => s.machine.name)).toEqual([a.name])
  })

  test("a newer green run is not judged stale by an older run's later-landing snapshot", () => {
    // The clock-domain regression: job A completes, job B completes after
    // it, THEN A's promotion snapshot lands. snapshotAt (capture time)
    // postdates B's completedAt (job time), but B's evidence is the newer
    // machine state and must still promote.
    const holder = machine(HOLDER, { status: "stopped" }) // holds A's seed
    const winnerB = machine(`p-run-${LINEAGE}-x2`)
    const plan = steps(
      world({
        machines: [holder, winnerB],
        // A's snapshot was captured 120s ago; B's job completed 180s ago -
        // after A's job, before A's snapshot existed.
        seeds: new Map([[holder.id, { holder, snapshotId: "s", snapshotAt: NOW - 120_000 }]]),
        queue: {
          demanded: [],
          finished: [finished(winnerB.name, { completedAt: (NOW - 180_000) / 1000 })],
          truncated: false,
        },
      }),
    )
    const promotes = only(plan, "promote")
    expect(promotes.map((s) => s.winner.name)).toEqual([winnerB.name])
    expect(only(plan, "delete")).toHaveLength(0)
  })

  test("promoting names the old holder for the swap", () => {
    const holder = machine(HOLDER, { status: "stopped" })
    const winner = machine(`p-run-${LINEAGE}-x1`)
    const plan = steps(
      world({
        machines: [holder, winner],
        seeds: new Map([[holder.id, { holder, snapshotId: "s", snapshotAt: NOW - 900_000 }]]),
        queue: { demanded: [], finished: [finished(winner.name)], truncated: false },
      }),
    )
    const promotes = only(plan, "promote")
    expect(promotes).toHaveLength(1)
    expect(promotes[0]!.oldHolder?.name).toBe(HOLDER)
  })
})

describe("cleanup", () => {
  test("a young unregistered runner is protected by the warm grace", () => {
    const young = machine(`p-run-${LINEAGE}-x1`, { createdAt: NOW - 60_000 })
    expect(steps(world({ machines: [young] }))).toHaveLength(0)
  })

  test("an old unregistered runner is deleted", () => {
    const done = machine(`p-run-${LINEAGE}-x1`)
    const plan = steps(world({ machines: [done] }))
    expect(only(plan, "delete").map((s) => s.machine.name)).toEqual([done.name])
  })

  // The seed race: a JIT runner deregisters seconds after its job ends,
  // but the finished-job observation can lag. Deleting on deregistration
  // alone destroys the very disk state promotion snapshots - so an
  // unevidenced runner is HELD until its evidence arrives or the idle
  // grace expires.
  test("a deregistered runner without finished evidence is held, not deleted", () => {
    const unevidenced = machine(`p-run-${LINEAGE}-x1`, { createdAt: NOW - 400_000 })
    expect(steps(world({ machines: [unevidenced] }))).toHaveLength(0)
  })

  test("finished evidence releases the job-finished delete", () => {
    const evidenced = machine(`p-run-${LINEAGE}-x1`, { createdAt: NOW - 400_000 })
    const plan = steps(
      world({
        machines: [evidenced],
        queue: {
          demanded: [],
          // A PR job (not promotable) so the machine deletes instead of promoting.
          finished: [finished(evidenced.name, { onDefaultBranch: false })],
          truncated: false,
        },
      }),
    )
    const deletes = only(plan, "delete")
    expect(deletes.map((s) => s.machine.name)).toEqual([evidenced.name])
    expect(deletes[0]?.why).toBe("job finished")
  })

  test("the idle grace is the backstop for evidence that never arrives", () => {
    const abandoned = machine(`p-run-${LINEAGE}-x1`, { createdAt: NOW - 901_000 })
    const plan = decide(config, world({ machines: [abandoned] }), NOW)
    const deletes = only([...plan.steps], "delete")
    expect(deletes.map((s) => s.machine.name)).toEqual([abandoned.name])
    expect(deletes[0]?.why).toBe("no registration past idle grace")
    // The evidence-less delete warns: a lost green run must be findable.
    expect(
      plan.notes.some((n) => n.level === "warn" && n.text.includes(`${abandoned.name}: deleting`)),
    ).toBe(true)
  })

  test("the evidence-loss warning stays quiet when the scan was truncated", () => {
    // A truncated scan makes evidence absence expected; the delete still
    // happens (the machine is past every grace), but crying wolf on every
    // busy-repo tick would drown the real eviction signal.
    const abandoned = machine(`p-run-${LINEAGE}-x1`, { createdAt: NOW - 901_000 })
    const plan = decide(
      config,
      world({ machines: [abandoned], queue: { demanded: [], finished: [], truncated: true } }),
      NOW,
    )
    expect(only([...plan.steps], "delete").map((s) => s.machine.name)).toEqual([abandoned.name])
    expect(plan.notes.some((n) => n.level === "warn" && n.text.includes("deleting"))).toBe(false)
  })

  test("a held runner promotes when its green default-branch evidence lands late", () => {
    const winner = machine(`p-run-${LINEAGE}-x1`, { createdAt: NOW - 400_000 })
    const plan = steps(
      world({
        machines: [winner],
        queue: { demanded: [], finished: [finished(winner.name)], truncated: false },
      }),
    )
    expect(only(plan, "promote").map((s) => s.winner.name)).toEqual([winner.name])
    expect(only(plan, "delete")).toHaveLength(0)
  })

  test("an offline registration with no machine is deregistered", () => {
    const orphan = registration(`p-run-${LINEAGE}-x1`, { online: false })
    const plan = steps(world({ registrations: [orphan] }))
    const deregisters = only(plan, "deregister")
    expect(deregisters).toHaveLength(1)
    expect(deregisters[0]!.registrationIds).toEqual([orphan.id])
  })

  test("an online machineless registration is left alone", () => {
    const plan = steps(world({ registrations: [registration(`p-run-${LINEAGE}-x1`)] }))
    expect(plan).toHaveLength(0)
  })

  test("a retiring holder is deleted only once its successor's seed is ready", () => {
    const retiring = machine(`${HOLDER}-retiring`, { status: "stopped" })
    const successor = machine(HOLDER, { status: "stopped" })
    const without = decide(
      config,
      world({
        machines: [retiring, successor],
        seeds: new Map([[successor.id, { holder: successor, snapshotId: undefined, snapshotAt: undefined }]]),
      }),
      NOW,
    )
    expect([...without.steps]).toHaveLength(0)
    expect(without.notes.some((n) => n.text.includes("retiring"))).toBe(true)

    const withReady = steps(
      world({
        machines: [retiring, successor],
        seeds: new Map([[successor.id, { holder: successor, snapshotId: "s", snapshotAt: NOW }]]),
      }),
    )
    expect(only(withReady, "delete").map((s) => s.machine.name)).toEqual([retiring.name])
  })
})

describe("retirement", () => {
  const standby = () =>
    machine(`p-run-${LINEAGE}-x1`, { createdAt: NOW - 2 * 900 * 1000 })

  test("an idle standby past the grace is retired on a scheduled tick", () => {
    const idle = standby()
    const plan = steps(world({ machines: [idle], registrations: [registration(idle.name)] }))
    const retires = only(plan, "retire")
    expect(retires).toHaveLength(1)
    expect(retires[0]!.machine.name).toBe(idle.name)
  })

  test("never on an event tick", () => {
    const idle = standby()
    const plan = steps(
      world({ machines: [idle], registrations: [registration(idle.name)] }),
      { ...config, mayScaleDown: false },
    )
    expect(only(plan, "retire")).toHaveLength(0)
  })

  test("not while demand wants it", () => {
    const idle = standby()
    const plan = steps(
      world({
        machines: [idle],
        registrations: [registration(idle.name)],
        queue: { demanded: [{ labels: [...LABELS] }], finished: [], truncated: false },
      }),
    )
    expect(only(plan, "retire")).toHaveLength(0)
  })

  test("not while the grace holds", () => {
    const young = machine(`p-run-${LINEAGE}-x1`, { createdAt: NOW - 60_000 })
    const plan = steps(world({ machines: [young], registrations: [registration(young.name)] }))
    expect(only(plan, "retire")).toHaveLength(0)
  })

  test("a busy runner is never retired", () => {
    const busy = machine(`p-run-${LINEAGE}-x1`, { createdAt: NOW - 10_000_000 })
    const plan = steps(
      world({ machines: [busy], registrations: [registration(busy.name, { busy: true })] }),
    )
    expect(only(plan, "retire")).toHaveLength(0)
  })
})

describe("failed machines", () => {
  test("are cleared, deregister-first when registered", () => {
    const dead = machine(`p-run-${LINEAGE}-x1`, { status: "failed", failureReason: "kernel oops" })
    const reg = registration(dead.name)
    const plan = steps(world({ machines: [dead], registrations: [reg] }))
    const retires = only(plan, "retire")
    expect(retires).toHaveLength(1)
    expect(retires[0]!.registrationIds).toEqual([reg.id])
    expect(retires[0]!.why).toContain("kernel oops")
  })
})

describe("min-warm", () => {
  test("holds standbys for a seeded lineage seen in the finished window", () => {
    const holder = machine(HOLDER, { status: "stopped" })
    const plan = steps(
      world({
        machines: [holder],
        seeds: new Map([[holder.id, { holder, snapshotId: "s", snapshotAt: NOW - 10_000_000 }]]),
        queue: { demanded: [], finished: [finished("gone-runner")], truncated: false },
      }),
      { ...config, minWarm: 2 },
    )
    expect(only(plan, "spawn")).toHaveLength(2)
  })

  test("cannot warm a lineage whose labels are out of the window", () => {
    const holder = machine(HOLDER, { status: "stopped" })
    const plan = steps(
      world({
        machines: [holder],
        seeds: new Map([[holder.id, { holder, snapshotId: "s", snapshotAt: NOW - 10_000_000 }]]),
      }),
      { ...config, minWarm: 2 },
    )
    expect(only(plan, "spawn")).toHaveLength(0)
  })
})

test("a rev roll deletes every old holder", () => {
  const stale = machine(`p-seed-${LINEAGE}-bbbbbbbb`, { status: "stopped" })
  const staleRetiring = machine(`p-seed-${LINEAGE}-bbbbbbbb-retiring`, { status: "stopped" })
  const plan = steps(world({ machines: [stale, staleRetiring] }))
  expect(only(plan, "delete")).toHaveLength(2)
})

test("truncated scans note that demand is a floor", () => {
  const plan = decide(config, world({ queue: { demanded: [], finished: [], truncated: true } }), NOW)
  expect(plan.notes.some((n) => n.text.includes("floor"))).toBe(true)
})

describe("holes the first review found, pinned shut", () => {
  test("a runner whose registrations never come online is retired after the idle grace", () => {
    const dark = machine(`p-run-${LINEAGE}-zzz1`, { createdAt: NOW - 1_000_000 })
    const plan = steps(
      world({ machines: [dark], registrations: [registration(dark.name, { online: false })] }),
    )
    const retires = only(plan, "retire")
    expect(retires).toHaveLength(1)
    expect(retires[0]!.machine.id).toBe(dark.id)
  })

  test("a dark runner younger than the idle grace is left alone", () => {
    const young = machine(`p-run-${LINEAGE}-zzz2`, { createdAt: NOW - 60_000 })
    const plan = steps(
      world({ machines: [young], registrations: [registration(young.name, { online: false })] }),
    )
    expect(plan).toHaveLength(0)
  })

  test("a dark runner is not reaped on a tick that could not read the queue", () => {
    const dark = machine(`p-run-${LINEAGE}-zzz3`, { createdAt: NOW - 1_000_000 })
    const plan = steps(
      world({
        machines: [dark],
        registrations: [registration(dark.name, { online: false })],
        queue: null,
      }),
    )
    expect(plan).toHaveLength(0)
  })

  test("a current holder found running is stopped", () => {
    const holder = machine(HOLDER)
    const plan = steps(world({ machines: [holder] }))
    const stops = only(plan, "stop")
    expect(stops).toHaveLength(1)
    expect(stops[0]!.machine.id).toBe(holder.id)
  })

  test("a stopped holder is not stopped again", () => {
    const holder = machine(HOLDER, { status: "stopped" })
    expect(only(steps(world({ machines: [holder] })), "stop")).toHaveLength(0)
  })

  test("promotion over a FAILED incumbent hands it over as the machine to move aside", () => {
    // With evidence-suffixed names a collision needs the failed incumbent
    // to hold EXACTLY the target name (same lineage, rev, and evidence
    // second) - rare, but a failed holder from a previous promotion of the
    // same-second evidence does exactly that.
    const incumbent = machine(seedName("p", LINEAGE, REV, NOW / 1000 - 60), { status: "failed" })
    const winner = machine(`p-run-${LINEAGE}-abc1`)
    const plan = steps(
      world({
        machines: [incumbent, winner],
        queue: { demanded: [], finished: [finished(winner.name)], truncated: false },
      }),
    )
    const promotes = only(plan, "promote")
    expect(promotes).toHaveLength(1)
    expect(promotes[0]!.oldHolder?.id).toBe(incumbent.id)
  })

  test("a failed machine's registrations are retired once, never also orphan-swept", () => {
    const dead = machine(`p-run-${LINEAGE}-zzz4`, { status: "failed" })
    const plan = steps(
      world({ machines: [dead], registrations: [registration(dead.name, { online: false })] }),
    )
    expect(only(plan, "retire")).toHaveLength(1)
    expect(only(plan, "deregister")).toHaveLength(0)
  })
})
