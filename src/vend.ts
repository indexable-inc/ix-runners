/** Where the GitHub admin credential comes from.
 *
 * Two eras, chosen by IX_TOKEN_SOURCE (the action's `token-source` input):
 *
 * - "ix": trade the run's OIDC identity for a repo-scoped GitHub App
 *   installation token, vended by the ix control plane. No standing admin
 *   secret exists anywhere: the repository comes from the OIDC token's
 *   signed claims, and GitHub owns the token's expiry.
 * - "pat": the admin PAT from GITHUB_ADMIN_TOKEN / RUNNER_PAT. The
 *   fallback era, for repositories without the ix-runners App installed.
 */

import type { Client } from "@indexable/sdk"
import { clean, logError, mask } from "./report.ts"

/** The audience ix's vending endpoint validates OIDC tokens against. */
const OIDC_AUDIENCE = "ix.dev"

/** The bearer token the GitHub half of the reconcile runs on. Exits with
 * the fix named rather than returning anything questionable. */
export async function adminCredential(ix: Client): Promise<string> {
  const source = process.env.IX_TOKEN_SOURCE || "pat"
  if (source === "ix") return await vend(ix)
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

/** OIDC -> installation token. The JWT is fetched from GitHub's own token
 * service and nowhere else: host pinned, https only, redirects refused -
 * the same posture github.ts takes, because this credential is worth the
 * same theft. */
async function vend(ix: Client): Promise<string> {
  const url = process.env.ACTIONS_ID_TOKEN_REQUEST_URL
  const bearer = process.env.ACTIONS_ID_TOKEN_REQUEST_TOKEN
  if (!url || !bearer) {
    logError(
      "token-source 'ix' needs the workflow's OIDC grant: add" +
        " `permissions: id-token: write` to the reconcile job.",
    )
    process.exit(1)
  }
  const endpoint = new URL(url)
  endpoint.searchParams.set("audience", OIDC_AUDIENCE)
  if (
    endpoint.protocol !== "https:" ||
    (endpoint.hostname !== "token.actions.githubusercontent.com" &&
      !endpoint.hostname.endsWith(".actions.githubusercontent.com"))
  ) {
    logError(`refusing OIDC endpoint ${clean(endpoint.origin)}: not GitHub's token service`)
    process.exit(1)
  }
  const response = await fetch(endpoint, {
    headers: { authorization: `Bearer ${bearer}`, accept: "application/json" },
    redirect: "manual", // a redirect here is exfiltration, not routing
  })
  if (response.status >= 300 && response.status < 400) {
    logError("refusing to follow a redirect from GitHub's OIDC token service")
    process.exit(1)
  }
  if (!response.ok) {
    logError(`OIDC token request failed (${response.status})`)
    process.exit(1)
  }
  const jwt = ((await response.json()) as { value?: string }).value
  if (!jwt) {
    logError("the OIDC token response carried no token")
    process.exit(1)
  }
  mask(jwt)
  const vended = await ix.ci().githubRunnerToken(jwt)
  mask(vended.token)
  return vended.token
}
