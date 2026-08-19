/** The executor's promote and spawn hygiene, pinned against fakes that
 * record every SDK call: which snapshot a retry adopts, and that no handle
 * outlives its step. */

import { afterAll, describe, expect, mock, test } from "bun:test"
import type { Client } from "@indexable/sdk"
import type { GitHub } from "./github.ts"
import type { MachineRow, Plan } from "./types.ts"

// The SDK's entry point loads a native addon at import time; the executor
// only needs the NotFound class from it, so tests substitute a plain one
// and never touch the network or the addon.
mock.module("@indexable/sdk", () => ({ NotFound: class NotFound extends Error {} }))
const { execute } = await import("./execute.ts")
// The module mock is process-global: restore it so no later-loaded test
// file inherits a fake SDK by accident.
afterAll(() => mock.restore())

const WINNER: MachineRow = {
  id: "w1",
  name: "p-run-aaaaaaaa-x1",
  status: "running",
  createdAt: 0,
}
const HOLDER_NAME = "p-seed-aaaaaaaa-bbbbbbbb"

interface FakeSnapshot {
  id: string
  status: string
  createdAt: number
}

/** A recording ix client: every machine call lands in `calls` as
 * `<verb>:<machine id>[:<argument>]`. */
function fakeIx(options: {
  snapshots?: FakeSnapshot[]
  wait?: string
  jitFails?: boolean
  listFails?: boolean
}) {
  const calls: string[] = []
  const handle = (id: string) => ({
    snapshot: async () => {
      calls.push(`snapshot:${id}`)
      return { snapshotId: "fresh" }
    },
    waitSnapshotReady: async (snapshotId: string) => {
      calls.push(`wait:${id}:${snapshotId}`)
      return options.wait ?? "ready"
    },
    rename: async (name: string) => {
      calls.push(`rename:${id}:${name}`)
    },
    stop: async () => {
      calls.push(`stop:${id}`)
    },
    delete: async () => {
      calls.push(`delete:${id}`)
    },
    close: async () => {
      calls.push(`close:${id}`)
    },
    writeFile: async () => {
      calls.push(`write:${id}`)
    },
    id: () => id,
  })
  const ix = {
    machines: () => ({
      connect: (id: string) => handle(id),
      create: async () => {
        calls.push("create:new")
        return handle("new")
      },
    }),
    snapshots: () => ({
      list: async (machineId: string) => {
        calls.push(`list:${machineId}`)
        if (options.listFails) throw new Error("listing outage")
        return options.snapshots ?? []
      },
    }),
  } as unknown as Client
  const gh = {
    mintJitConfig: async () => {
      if (options.jitFails) throw new Error("boom")
      return "jit-blob"
    },
  } as unknown as GitHub
  return { ix, gh, calls }
}

const promotePlan: Plan = {
  steps: [{ do: "promote", winner: WINNER, holderName: HOLDER_NAME, oldHolder: undefined }],
  notes: [],
}

describe("promote adopts prior captures", () => {
  test("a still-capturing snapshot from a previous attempt is waited on, not re-minted", async () => {
    const { ix, gh, calls } = fakeIx({
      snapshots: [{ id: "prev", status: "capturing", createdAt: Date.now() - 60_000 }],
    })
    const outcome = await execute(ix, gh, promotePlan)
    expect(outcome.failures).toBe(0)
    expect(calls).toContain("wait:w1:prev")
    expect(calls.some((call) => call.startsWith("snapshot:"))).toBe(false)
    expect(calls).toContain(`rename:w1:${HOLDER_NAME}`)
  })

  test("a ready snapshot is adopted over a younger in-flight one", async () => {
    const { ix, gh, calls } = fakeIx({
      snapshots: [
        { id: "done", status: "ready", createdAt: Date.now() - 120_000 },
        { id: "mid", status: "capturing", createdAt: Date.now() - 60_000 },
      ],
    })
    await execute(ix, gh, promotePlan)
    expect(calls).toContain("wait:w1:done")
  })

  test("failed and gone snapshots are never reused; a fresh one is minted", async () => {
    const { ix, gh, calls } = fakeIx({
      snapshots: [{ id: "dead", status: "failed", createdAt: 9 }],
    })
    await execute(ix, gh, promotePlan)
    expect(calls).toContain("snapshot:w1")
    expect(calls).toContain("wait:w1:fresh")
  })

  test("a STUCK capturing snapshot (older than wait + a tick) is not re-adopted", async () => {
    // One wedged capturing row must not be waited on every tick forever.
    const { ix, gh, calls } = fakeIx({
      snapshots: [{ id: "stuck", status: "capturing", createdAt: Date.now() - 3_600_000 }],
    })
    await execute(ix, gh, promotePlan)
    expect(calls).toContain("snapshot:w1")
    expect(calls).toContain("wait:w1:fresh")
  })

  test("a snapshot-listing failure falls back to minting, never fails the promote", async () => {
    const { ix, gh, calls } = fakeIx({ listFails: true })
    const outcome = await execute(ix, gh, promotePlan)
    expect(outcome.failures).toBe(0)
    expect(calls).toContain("snapshot:w1")
  })

  test("a wait that ends short of ready fails the step, renames nothing, releases the handle", async () => {
    const { ix, gh, calls } = fakeIx({
      snapshots: [{ id: "prev", status: "capturing", createdAt: Date.now() - 60_000 }],
      wait: "gone",
    })
    const outcome = await execute(ix, gh, promotePlan)
    expect(outcome.failures).toBe(1)
    expect(calls.some((call) => call.startsWith("rename:"))).toBe(false)
    expect(calls).toContain("close:w1")
  })
})

describe("spawn handle ownership", () => {
  const spawnPlan: Plan = {
    steps: [
      {
        do: "spawn",
        name: "p-run-aaaaaaaa-x9",
        labels: ["ix"],
        source: { template: "github:acme/app/rev#ci-runner" },
        region: "us-west-1",
      },
    ],
    notes: [],
  }

  test("a failed mint DELETES the half-spawned machine explicitly, then closes the handle", async () => {
    const { ix, gh, calls } = fakeIx({ jitFails: true })
    const outcome = await execute(ix, gh, spawnPlan)
    expect(outcome.failures).toBe(1)
    // The delete verb is pinned: cleanup must never lean on close()'s
    // SDK-ownership side effect to do the billing-critical deletion.
    expect(calls).toContain("delete:new")
    expect(calls).toContain("close:new")
  })

  test("a successful spawn never deletes or closes the create handle", async () => {
    const { ix, gh, calls } = fakeIx({})
    const outcome = await execute(ix, gh, spawnPlan)
    expect(outcome.failures).toBe(0)
    expect(calls).toContain("write:new")
    expect(calls).not.toContain("delete:new")
    expect(calls).not.toContain("close:new")
  })
})
