/** One tick's reading of the world. Observation only: nothing here decides
 * anything or changes anything, so a failure here can only ever cost a
 * tick, never capacity. */

import type { Client, MachineInfo } from "@indexable/sdk"
import type { Config } from "./config.ts"
import { resolveRev } from "./config.ts"
import type { GitHub } from "./github.ts"
import { parseRole } from "./names.ts"
import { logWarning } from "./report.ts"
import type { MachineRow, Seed, World } from "./types.ts"

export async function observe(ix: Client, gh: GitHub, config: Config): Promise<World> {
  // Independent reads, one round trip deep.
  const [rev, defaultBranch, machineInfos, allRegistrations] = await Promise.all([
    resolveRev(config),
    gh.defaultBranch(),
    listMachinesCompletely(ix),
    gh.listRunners(),
  ])

  // Only rows whose NAME parses as this pool's are this pool's: the name
  // codec is the sole membership test, so a human's unrelated machines and
  // other pools on the same account are invisible rather than at risk.
  const machines: MachineRow[] = machineInfos
    .filter((info) => parseRole(config.pool, info.name) !== undefined)
    .map((info) => ({
      id: info.id,
      name: info.name,
      status: info.status,
      createdAt: info.createdAt,
      failureReason: info.failureReason ?? undefined,
    }))
  const registrations = allRegistrations.filter(
    (registration) => parseRole(config.pool, registration.name) !== undefined,
  )

  const queue = await gh.observeQueue(config.runnerLabel, defaultBranch)

  // Every holder's newest restorable snapshot, retiring holders included:
  // crash recovery needs to know which of two holders carries the fresher
  // seed, and a stopped holder has no guest to ask - the snapshot listing
  // is the only channel.
  const seeds = new Map<string, Seed>()
  const holders = machines.filter(
    (machine) => parseRole(config.pool, machine.name)?.kind === "seed",
  )
  await Promise.all(
    holders.map(async (holder) => {
      const snapshots = await ix.snapshots().list(holder.id)
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

  return { rev, defaultBranch, machines, registrations, queue, seeds }
}

/** The machine listing, cross-checked against itself.
 *
 * The runner listing refuses short reads (GitHub says how many rows exist);
 * the machine listing has no such receipt, and the platform is KNOWN to
 * return transiently partial listings. A missing row is the destructive
 * direction: a held winner outside the world model is invisible to every
 * rule and strands a billing machine, while a stale extra row at worst
 * plans a delete that no-ops on NotFound. So: read twice, and on any
 * disagreement read once more and take the UNION of everything seen,
 * naming what flickered. */
async function listMachinesCompletely(ix: Client): Promise<MachineInfo[]> {
  const reads: MachineInfo[][] = await Promise.all([ix.machines().list(), ix.machines().list()])
  if (flickeringIds(reads).length === 0) return reads[1]!
  reads.push(await ix.machines().list())
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
