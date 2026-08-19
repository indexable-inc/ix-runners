import { describe, expect, test } from "bun:test"
import { canonicalLabels, lineageKey, parseRole, runnerName, seedName } from "./names.ts"

describe("lineageKey", () => {
  test("is order- and duplicate-insensitive", () => {
    expect(lineageKey(["ix", "16vcpu"])).toBe(lineageKey(["16vcpu", "ix", "16vcpu"]))
  })
  test("distinct label sets get distinct keys", () => {
    expect(lineageKey(["ix"])).not.toBe(lineageKey(["ix", "16vcpu"]))
  })
  test("is 8 hex chars", () => {
    expect(lineageKey(["ix"])).toMatch(/^[0-9a-f]{8}$/)
  })
})

describe("the name codec round-trips", () => {
  const lineage = lineageKey(["ix"])
  const rev = "0123456789abcdef0123456789abcdef01234567"

  test("seed names", () => {
    expect(parseRole("baml", seedName("baml", lineage, rev))).toEqual({
      kind: "seed",
      lineage,
      rev: rev.slice(0, 8),
      retiring: false,
    })
  })
  test("retiring seed names", () => {
    expect(parseRole("baml", `${seedName("baml", lineage, rev)}-retiring`)).toEqual({
      kind: "seed",
      lineage,
      rev: rev.slice(0, 8),
      retiring: true,
    })
  })
  test("runner names", () => {
    expect(parseRole("baml", runnerName("baml", lineage))).toEqual({ kind: "runner", lineage })
  })
  test("a pool name containing dashes still parses", () => {
    const pool = "my-cool-pool"
    expect(parseRole(pool, runnerName(pool, lineage))).toEqual({ kind: "runner", lineage })
  })
})

describe("foreign names are invisible", () => {
  test.each([
    "unrelated-machine",
    "baml-run", // no lineage
    "baml-seed-0123abcd", // no rev
    "otherpool-run-0123abcd-xyz",
    "baml-run-0123abcd-xyz-extra suffix",
    "bamlx-run-0123abcd-xyz", // prefix must match exactly
  ])("%s", (name) => {
    expect(parseRole("baml", name)).toBeUndefined()
  })
})

test("canonicalLabels sorts and dedups", () => {
  expect(canonicalLabels(["b", "a", "b"])).toEqual(["a", "b"])
})
