/** The executor's promote and spawn hygiene, pinned against fakes that
 * record every SDK call: which snapshot a retry adopts, and that no handle
 * outlives its step. */

import { describe, expect, mock, test } from "bun:test"
import type { Client } from "@indexable/sdk"
import type { GitHub } from "./github.ts"
import type { MachineRow, Plan } from "./types.ts"

// The SDK's entry point loads a native addon at import time; the executor
// only needs the NotFound class from it, so tests substitute a plain one
// and never touch the network or the addon.
mock.module("@indexable/sdk", () => ({ NotFound: class NotFound extends Error {} }))
const { execute } = await import("./execute.ts")

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
function fakeIx(options: { snapshots?: FakeSnapshot[]; wait?: string; jitFails?: boolean }) {
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
      snapshots: [{ id: "prev", status: "capturing", createdAt: 5 }],
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
        { id: "done", status: "ready", createdAt: 1 },
        { id: "mid", status: "capturing", createdAt: 2 },
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

  test("a wait that ends short of ready fails the step, renames nothing, releases the handle", async () => {
    const { ix, gh, calls } = fakeIx({
      snapshots: [{ id: "prev", status: "capturing", createdAt: 5 }],
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

  test("a failed mint closes the create handle, which deletes the machine it owns", async () => {
    const { ix, gh, calls } = fakeIx({ jitFails: true })
    const outcome = await execute(ix, gh, spawnPlan)
    expect(outcome.failures).toBe(1)
    expect(calls).toContain("close:new")
  })

  test("a successful spawn never closes the create handle: close would delete the machine", async () => {
    const { ix, gh, calls } = fakeIx({})
    const outcome = await execute(ix, gh, spawnPlan)
    expect(outcome.failures).toBe(0)
    expect(calls).toContain("write:new")
    expect(calls).not.toContain("close:new")
  })
})
