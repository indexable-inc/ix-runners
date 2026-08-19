/** The listing cross-check, pinned on its pure halves: which ids count as
 * flickering, and how the union reassembles a partial read. */

import { describe, expect, test } from "bun:test"
import { flickeringIds, unionListings } from "./observe.ts"

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
