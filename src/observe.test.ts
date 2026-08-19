/** The listing cross-check, pinned on its pure halves (which ids count as
 * flickering, how the union reassembles a partial read), on its composition
 * (disagree -> third read -> union), and on the holder probe's tolerance of
 * rows the listing has already been disproven about. */

import { afterAll, describe, expect, mock, test } from "bun:test"
import type { Client } from "@indexable/sdk"
import type { Config } from "./config.ts"
import type { GitHub } from "./github.ts"

// The SDK's entry point loads a native addon at import time; observe only
// needs the NotFound class from it, so tests substitute a plain one and
// never touch the network or the addon.
mock.module("@indexable/sdk", () => ({ NotFound: class NotFound extends Error {} }))
const { NotFound } = await import("@indexable/sdk")
const { flickeringIds, listCompletely, observe, unionListings } = await import("./observe.ts")
// The module mock is process-global: restore it so no later-loaded test
// file inherits a fake SDK by accident.
afterAll(() => mock.restore())

const row = (id: string, status = "running") => ({ id, name: `m-${id}`, status })

describe("the machine-listing cross-check", () => {
  test("agreeing reads flicker nothing", () => {
    const reads = [
      [row("a"), row("b")],
      [row("b"), row("a")], // order is not identity
    ]
    expect(flickeringIds(reads)).toEqual([])
  })

  test("a row missing from one read is flickering", () => {
    expect(flickeringIds([[row("a"), row("b")], [row("a")]])).toEqual(["b"])
  })

  test("flicker is symmetric: extra rows count the same as missing ones", () => {
    expect(flickeringIds([[row("a")], [row("a"), row("c")]])).toEqual(["c"])
  })

  test("the union keeps every row seen by any read, once", () => {
    const reads = [[row("a"), row("b")], [row("b"), row("c")], [row("a")]]
    expect(unionListings(reads).map((r) => r.id).sort()).toEqual(["a", "b", "c"])
  })

  test("later reads win on a row's contents", () => {
    const reads = [[row("a", "running")], [row("a", "stopped")]]
    expect(unionListings(reads)).toEqual([row("a", "stopped")])
  })
})

describe("listCompletely composes the cross-check", () => {
  test("agreeing reads stop at two, no third read", async () => {
    let reads = 0
    const rows = await listCompletely(
      async () => {
        reads += 1
        return [row("a"), row("b")]
      },
      () => true,
    )
    expect(reads).toBe(2)
    expect(rows.map((r) => r.id).sort()).toEqual(["a", "b"])
  })

  test("a disagreement forces a third read and unions everything seen", async () => {
    const sequence = [[row("a"), row("b")], [row("a")], [row("a"), row("c")]]
    let reads = 0
    const rows = await listCompletely(async () => sequence[reads++]!, () => true)
    expect(reads).toBe(3)
    expect(rows.map((r) => r.id).sort()).toEqual(["a", "b", "c"])
  })

  test("foreign rows are filtered out BEFORE the comparison, so their churn never trips it", async () => {
    const sequence = [
      [row("a"), row("noise-1")],
      [row("a"), row("noise-2")], // foreign churn between reads
    ]
    let reads = 0
    const rows = await listCompletely(
      async () => sequence[reads++]!,
      (r) => !r.id.startsWith("noise"),
    )
    expect(reads).toBe(2)
    expect(rows.map((r) => r.id)).toEqual(["a"])
  })
})

describe("observe survives rows the listing already lied about", () => {
  const config = {
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
    // A pinned rev keeps resolveRev off git entirely.
    templateRev: "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  } satisfies Config

  const gh = {
    defaultBranch: async () => "main",
    listRunners: async () => [],
    observeQueue: async () => ({ demanded: [], finished: [], truncated: false }),
  } as unknown as GitHub

  test("a holder answering NotFound to the snapshot probe is dropped, not fatal", async () => {
    const holder = {
      id: "h1",
      name: "p-seed-00000000-aaaaaaaa-abc123",
      status: "stopped",
      createdAt: 1,
    }
    const runner = { id: "r1", name: "p-run-00000000-x1", status: "running", createdAt: 2 }
    const ix = {
      machines: () => ({ list: async () => [holder, runner] }),
      snapshots: () => ({
        list: async () => {
          throw new NotFound("machine not found")
        },
      }),
    } as unknown as Client
    const world = await observe(ix, gh, config)
    expect(world.machines.map((m) => m.id)).toEqual(["r1"])
    expect(world.seeds.size).toBe(0)
  })

  test("a non-NotFound snapshot-probe failure still fails observation", async () => {
    const holder = {
      id: "h1",
      name: "p-seed-00000000-aaaaaaaa-abc123",
      status: "stopped",
      createdAt: 1,
    }
    const ix = {
      machines: () => ({ list: async () => [holder] }),
      snapshots: () => ({
        list: async () => {
          throw new Error("transport outage")
        },
      }),
    } as unknown as Client
    await expect(observe(ix, gh, config)).rejects.toThrow("transport outage")
  })
})
