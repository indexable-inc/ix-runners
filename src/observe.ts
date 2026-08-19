/** One tick's reading of the world. Observation only: nothing here decides
 * anything or changes anything, so a failure here can only ever cost a
 * tick, never capacity. */

import type { Client } from "@indexable/sdk"
import { NotFound } from "@indexable/sdk"
import type { Config } from "./config.ts"
import { resolveRev } from "./config.ts"
import type { GitHub } from "./github.ts"
import { parseRole } from "./names.ts"
import { logWarning } from "./report.ts"
import type { MachineRow, Seed, World } from "./types.ts"

/** One cron tick, the slack every time-bounded read allows. */
const ONE_TICK_MS = 15 * 60_000

export async function observe(ix: Client, gh: GitHub, config: Config): Promise<World> {
  // Independent reads, one round trip deep.
  const [rev, defaultBranch, machineInfos, allRegistrations] = await Promise.all([
    resolveRev(config),
    gh.defaultBranch(),
    listCompletely(
      () => ix.machines().list(),
      // Only rows whose NAME parses as this pool's are this pool's: the
      // name codec is the sole membership test, so a human's unrelated
      // machines and other pools on the same account are invisible rather
      // than at risk - and their churn never trips the cross-check.
      (info) => parseRole(config.pool, info.name) !== undefined,
    ),
    gh.listRunners(),
  ])

  let machines: MachineRow[] = machineInfos.map((info) => ({
    id: info.id,
    name: info.name,
    status: info.status,
    createdAt: info.createdAt,
    failureReason: info.failureReason ?? undefined,
  }))
  const registrations = allRegistrations.filter(
    (registration) => parseRole(config.pool, registration.name) !== undefined,
  )

  // Finished-run evidence only matters for machines that still exist, and
  // the runs listing is newest-first: no completed run created before the
  // oldest standing runner (minus a tick of slack) can name one, so the
  // completed scan stops there instead of burning a fixed request budget.
  // No standing runners keeps the last tick's window, which is what lets
  // min-warm re-teach labels from recent activity. Known gap, accepted: a
  // run that sat QUEUED longer than a tick before its machine spawned has
  // an old created_at and can fall under the floor; the idle-grace delete
  // warns when that costs evidence.
  const runnerRows = machines.filter(
    (machine) => parseRole(config.pool, machine.name)?.kind === "runner",
  )
  const evidenceFloorMs =
    (runnerRows.length > 0
      ? Math.min(...runnerRows.map((machine) => machine.createdAt))
      : Date.now()) - ONE_TICK_MS

  const queue = await gh.observeQueue(config.runnerLabel, defaultBranch, evidenceFloorMs)

  // Every holder's newest restorable snapshot, retiring holders included:
  // crash recovery needs to know which of two holders carries the fresher
  // seed, and a stopped holder has no guest to ask - the snapshot listing
  // is the only channel. A holder deleted since the machine listing (a
  // concurrent tick's cleanup, or a stale row the union kept) answers
  // NotFound here: that is the row being disproven, not a failure - it is
  // dropped from the world rather than killing the whole tick.
  const seeds = new Map<string, Seed>()
  const gone = new Set<string>()
  const holders = machines.filter(
    (machine) => parseRole(config.pool, machine.name)?.kind === "seed",
  )
  await Promise.all(
    holders.map(async (holder) => {
      let snapshots
      try {
        snapshots = await ix.snapshots().list(holder.id)
      } catch (error) {
        if (error instanceof NotFound) {
          gone.add(holder.id)
          return
        }
        throw error
      }
      const ready = snapshots
        .filter((snapshot) => snapshot.status === "ready")
        .sort((a, b) => b.createdAt - a.createdAt)[0]
      seeds.set(holder.id, {
        holder,
        snapshotId: ready?.id,
        snapshotAt: ready?.createdAt,
      })
    }),
  )
  machines = machines.filter((machine) => !gone.has(machine.id))

  return { rev, defaultBranch, machines, registrations, queue, seeds }
}

/** A listing, cross-checked against itself.
 *
 * The runner listing refuses short reads (GitHub says how many rows exist);
 * the machine listing has no such receipt, and the platform is KNOWN to
 * return transiently partial listings. A missing row is the destructive
 * direction: a held winner outside the world model is invisible to every
 * rule and strands a billing machine, while a stale extra row is later
 * disproven wherever it is touched (the holder snapshot probe drops it,
 * deletes no-op on NotFound). So: read twice, filtered to this pool's rows
 * BEFORE comparing, and on any disagreement read once more and take the
 * UNION of everything seen, naming what flickered. */
export async function listCompletely<T extends { id: string; name: string }>(
  list: () => Promise<T[]>,
  member: (row: T) => boolean,
): Promise<T[]> {
  const first = await Promise.all([list(), list()])
  const reads = first.map((read) => read.filter(member))
  if (flickeringIds(reads).length === 0) return reads[1]!
  reads.push((await list()).filter(member))
  const flickered = new Set(flickeringIds(reads))
  const rows = unionListings(reads)
  const names = rows
    .filter((row) => flickered.has(row.id))
    .map((row) => `${row.name} (${row.id})`)
    .join(", ")
  logWarning(
    `the machine listing is flickering (a known transient partial-listing bug);` +
      ` reconciling over the union of ${reads.length} reads. Flickered: ${names}`,
  )
  return rows
}

/** Ids absent from at least one read: the flicker the union repairs. */
export function flickeringIds<T extends { id: string }>(
  reads: readonly (readonly T[])[],
): string[] {
  const union = new Set(reads.flatMap((read) => read.map((row) => row.id)))
  const flickering = new Set<string>()
  for (const read of reads) {
    const seen = new Set(read.map((row) => row.id))
    for (const id of union) if (!seen.has(id)) flickering.add(id)
  }
  return [...flickering].sort()
}

/** Every row seen by any read, one per id; later reads win on status. */
export function unionListings<T extends { id: string }>(
  reads: readonly (readonly T[])[],
): T[] {
  const rows = new Map<string, T>()
  for (const read of reads) for (const row of read) rows.set(row.id, row)
  return [...rows.values()]
}
