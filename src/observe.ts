/** One tick's reading of the world. Observation only: nothing here decides
 * anything or changes anything, so a failure here can only ever cost a
 * tick, never capacity. */

import type { Client } from "@indexable/sdk"
import type { Config } from "./config.ts"
import { resolveRev } from "./config.ts"
import type { GitHub } from "./github.ts"
import { parseRole } from "./names.ts"
import type { MachineRow, Seed, World } from "./types.ts"

export async function observe(ix: Client, gh: GitHub, config: Config): Promise<World> {
  // Independent reads, one round trip deep.
  const [rev, defaultBranch, machineInfos, allRegistrations] = await Promise.all([
    resolveRev(config),
    gh.defaultBranch(),
    ix.machines().list(),
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
