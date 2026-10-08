/** Prints the names of the e2e pool's run machines still on the IX_TOKEN
 * account, one per line, and exits 0; the cleanup job loops until this
 * prints nothing. Seeds (ixr-e2e-seed-*) are deliberately excluded: a green
 * run on main promotes one, and it is what the next e2e forks from.
 * Membership is the name codec, as in observe.ts. */

import { Client } from "@indexable/sdk"
import { parseRole } from "./names.ts"

const POOL = "ixr-e2e"

const machines = await new Client().machines().list()
for (const machine of machines) {
  const role = parseRole(POOL, machine.name)
  if (role !== undefined && role.kind === "runner") console.log(machine.name)
}
