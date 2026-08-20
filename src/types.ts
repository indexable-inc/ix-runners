/** The typed shapes the reconcile passes around. Everything here is data:
 * no I/O, no clock reads except the ones handed a timestamp. */

/** A `runs-on` label set: sorted, deduplicated, exact. Identity of a lineage. */
export type Labels = readonly string[]

/** One machine row as this tick found it, already known to be this pool's. */
export interface MachineRow {
  readonly id: string
  readonly name: string
  /** Lifecycle status, lowercased. Anything unrecognized is skipped, never acted on. */
  readonly status: string
  /** Epoch ms. */
  readonly createdAt: number
  readonly failureReason?: string | undefined
}

/** One runner registration as GitHub lists it. */
export interface Registration {
  readonly id: number
  /** Equals the machine name that carries it: minted that way at spawn. */
  readonly name: string
  readonly online: boolean
  readonly busy: boolean
}

/** A job this pool should serve, still waiting or running. */
export interface DemandedJob {
  readonly labels: Labels
}

/** A completed job in the scan window, as evidence. */
export interface FinishedJob {
  readonly runnerName: string
  readonly labels: Labels
  /** Epoch seconds. */
  readonly completedAt: number
  readonly succeeded: boolean
  /** Ran on the repository's default branch (the only promotions allowed). */
  readonly onDefaultBranch: boolean
}

/** What the GitHub queue scan learned. `null` at the World level means the
 * scan failed and this tick must not scale down. */
export interface QueueObservation {
  readonly demanded: readonly DemandedJob[]
  readonly finished: readonly FinishedJob[]
  /** More active runs than the scan reads: demand is a floor, not a count. */
  readonly truncated: boolean
}

/** A seed holder machine together with its restorable snapshot, if any. */
export interface Seed {
  readonly holder: MachineRow
  /** Newest ready snapshot id, or undefined while capture is in flight. */
  readonly snapshotId: string | undefined
  /** Epoch ms of that snapshot. */
  readonly snapshotAt: number | undefined
}

/** Everything one tick observed, before any decision is taken. */
export interface World {
  /** The runner-config rev every lineage is supposed to descend from. */
  readonly rev: string
  /** The repository's default branch: the only ref allowed to steer. */
  readonly defaultBranch: string
  readonly machines: readonly MachineRow[]
  readonly registrations: readonly Registration[]
  readonly queue: QueueObservation | null
  /** Holder machine id -> its seed reading (observe resolves snapshots). */
  readonly seeds: ReadonlyMap<string, Seed>
}

/** One thing to do. The decider emits these; only the executor makes any true.
 * Each step is self-contained: the executor never reaches back into the
 * World or re-derives anything the decision already knew. */
export type Step =
  | {
      readonly do: "spawn"
      readonly name: string
      readonly labels: Labels
      /** Restore the lineage's seed, or cold-boot the pinned template. */
      readonly source: { readonly snapshot: string } | { readonly template: string }
      /** The holder whose snapshot `source` restores, when it does. The
       * executor retires it in place if the platform refuses the snapshot
       * as not restorable - the only channel back to a stateless next tick
       * is the holder's name. */
      readonly seedHolder?: MachineRow | undefined
      readonly region: string
    }
  | {
      /** Snapshot the winner, swap it into the holder name, stop it. */
      readonly do: "promote"
      readonly winner: MachineRow
      /** The winner's green job completion, epoch SECONDS. No snapshot
       * created before it can hold that run's state, so it is the floor
       * for capture adoption. */
      readonly completedAtSec: number
      readonly holderName: string
      readonly oldHolder?: MachineRow | undefined
    }
  | { readonly do: "delete"; readonly machine: MachineRow; readonly why: string }
  | {
      /** A holder found running: a promotion died between rename and stop. */
      readonly do: "stop"
      readonly machine: MachineRow
      readonly why: string
    }
  | {
      /** Remove registrations that no longer have a machine behind them. */
      readonly do: "deregister"
      readonly name: string
      readonly registrationIds: readonly number[]
      readonly why: string
    }
  | {
      /** Deregister-first delete of a registered standby: GitHub's
       * busy-refusal (422) is the lock that makes this safe. */
      readonly do: "retire"
      readonly machine: MachineRow
      readonly registrationIds: readonly number[]
      readonly why: string
    }

/** The whole decision, as data. Notes travel with it so the log a run
 * produces is a rendering of the decision, not a side effect of deciding. */
export interface Plan {
  readonly steps: readonly Step[]
  readonly notes: readonly { readonly level: "info" | "warn"; readonly text: string }[]
}
