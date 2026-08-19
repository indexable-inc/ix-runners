/** Where the GitHub admin credential comes from.
 *
 * Two eras, chosen by IX_TOKEN_SOURCE (the action's `token-source` input):
 *
 * - "pat": the admin PAT from GITHUB_ADMIN_TOKEN / RUNNER_PAT. The working
 *   era, and for now the only one.
 * - "ix": trade the run's OIDC identity for a repo-scoped App installation
 *   token. NOT AVAILABLE YET from this codebase: vending is an in-protocol
 *   RPC in the ix SDK core - not a REST endpoint anywhere - and
 *   @indexable/sdk does not carry the `ci()` namespace the Python SDK has.
 *   Reverse-engineering the RPC frame here would be a workaround with an
 *   expiry date; refusing with the real blocker named is not.
 */

import { logError } from "./report.ts"

function tokenSource(): string {
  return process.env.IX_TOKEN_SOURCE || "pat"
}

/** The bearer token the GitHub half of the reconcile runs on. Exits with
 * the fix named rather than returning anything questionable. */
export function adminCredential(): string {
  const source = tokenSource()
  if (source === "ix") {
    logError(
      "token-source 'ix' is not available yet: @indexable/sdk does not" +
        " expose the ci() vending namespace (the Python ix-sdk does). Use" +
        " token-source: pat until the TypeScript SDK ships it.",
    )
    process.exit(1)
  }
  if (source !== "pat") {
    logError(`token-source must be 'ix' or 'pat', got '${source}'`)
    process.exit(1)
  }
  const pat = process.env.GITHUB_ADMIN_TOKEN || process.env.RUNNER_PAT
  if (!pat) {
    logError(
      "token-source is 'pat' but no admin credential is set: set the" +
        " action's runner-pat input (a fine-grained PAT with Administration" +
        " read/write on this repository).",
    )
    process.exit(1)
  }
  return pat
}
