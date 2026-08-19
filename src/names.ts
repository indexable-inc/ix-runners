/** The name codec: every machine's role, lineage and rev ride its NAME, so
 * the reconcile derives the whole world from listings and keeps no state.
 *
 *   <pool>-seed-<lineage8>-<rev8>-<evidence>            stopped anchor holding the seed snapshot
 *   <pool>-seed-<lineage8>-<rev8>-<evidence>-retiring   old holder mid-promotion, delete when safe
 *   <pool>-run-<lineage8>-<nonce>                       one runner, one job, then gone
 *
 * `<evidence>` is the seed's SOURCE JOB completion time, epoch seconds in
 * base36 (6-7 chars until the year 4453): the freshness gate compares
 * job time to job time, and the name is the reconcile's only state store.
 * Holders named before the suffix existed still parse, with the evidence
 * unknown; a rev roll washes them out. */

import type { Labels } from "./types.ts"

/** First 8 hex chars of SHA-256 over the sorted label set. */
export function lineageKey(labels: Labels): string {
  const canonical = [...new Set(labels)].sort().join("\n")
  const digest = new Bun.CryptoHasher("sha256").update(canonical).digest("hex")
  return digest.slice(0, 8)
}

/** Sorted, deduplicated - the canonical form every comparison uses. */
export function canonicalLabels(labels: readonly string[]): Labels {
  return [...new Set(labels)].sort()
}

export function seedName(
  pool: string,
  lineage: string,
  rev: string,
  /** The source job's completion time, epoch SECONDS. */
  evidenceAtSec?: number,
): string {
  const base = `${pool}-seed-${lineage}-${rev.slice(0, 8)}`
  if (evidenceAtSec === undefined) return base
  return `${base}-${Math.floor(evidenceAtSec).toString(36)}`
}

export function runnerName(pool: string, lineage: string): string {
  // Time-sortable nonce: two spawns in one tick cannot collide, and the
  // suffix never needs to be parsed back beyond "is a nonce".
  const nonce = Date.now().toString(36) + Math.random().toString(36).slice(2, 6)
  return `${pool}-run-${lineage}-${nonce}`
}

export type Role =
  | {
      kind: "seed"
      lineage: string
      rev: string
      retiring: boolean
      /** Source-job completion, epoch seconds; undefined on pre-suffix names. */
      evidenceAtSec: number | undefined
    }
  | { kind: "runner"; lineage: string }

/** Parse a machine name; undefined for anything that is not this pool's. */
export function parseRole(pool: string, name: string): Role | undefined {
  // "-retiring" is stripped BEFORE the evidence suffix is read: the word is
  // itself valid base36, so a single regex would eat it as evidence.
  const retiring = name.endsWith("-retiring")
  const bare = retiring ? name.slice(0, -"-retiring".length) : name
  const seed = bare.match(
    new RegExp(`^${escape(pool)}-seed-([0-9a-f]{8})-([0-9a-f]{8})(?:-([0-9a-z]{1,13}))?$`),
  )
  if (seed) {
    const evidence = seed[3] === undefined ? NaN : parseInt(seed[3], 36)
    return {
      kind: "seed",
      lineage: seed[1]!,
      rev: seed[2]!,
      retiring,
      evidenceAtSec: Number.isFinite(evidence) ? evidence : undefined,
    }
  }
  const run = name.match(new RegExp(`^${escape(pool)}-run-([0-9a-f]{8})-[0-9a-z]+$`))
  if (run) return { kind: "runner", lineage: run[1]! }
  return undefined
}

function escape(text: string): string {
  return text.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")
}
