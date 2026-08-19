/** Every knob, resolved once, and the rules that decide a knob is wrong.
 *
 * The spec file is TOML because it is read far more often than written and
 * every key wants a comment. Unknown keys are refused: a typo that silently
 * defaults is a pool quietly running someone else's numbers. */

import { logError } from "./report.ts"

/** Paths whose last-touching commit defines the runner-config rev. With
 * `flake-dir` set, the runner config IS that directory and only it rolls
 * the fleet. */
const CONFIG_PATHS = ["nix/", "flake.nix", "flake.lock"]

export interface Config {
  readonly repo: string
  readonly pool: string
  /** Flake attribute the cold-boot template comes from. */
  readonly templateAttr: string
  /** Subflake directory, "" for the repo flake. */
  readonly flakeDir: string
  /** Regions, in spec order; a lineage homes by hash modulo the list. */
  readonly regions: readonly string[]
  /** Marker label that opts a job into this pool. */
  readonly runnerLabel: string
  /** Global cap on concurrently existing runner machines. */
  readonly maxRunners: number
  /** Idle standbys to keep beyond queued demand, per lineage with demand. */
  readonly headroom: number
  /** Idle standbys to keep per known lineage even with no demand. */
  readonly minWarm: number
  /** Seconds an idle standby survives past min-warm before retirement. */
  readonly idleGraceSeconds: number
  /** Cold template boots admitted per tick: the bad-template-rev throttle. */
  readonly maxColdBoots: number
  /** Seconds a young machine's silence proves nothing. */
  readonly warmGraceSeconds: number
  /** Only a scheduled tick may retire capacity. */
  readonly mayScaleDown: boolean
  /** Where the cold-boot template builds from. Equal to `repo` (the
   * customer repository) normally; the ACTION's own repository in pool
   * mode, where the pool ships under pools/<name> in this repo. Kept apart
   * from `repo` because everything GitHub-side - runners, queue, JIT
   * credentials - stays on the customer repository either way. */
  readonly templateRepo: string
  /** The exact commit templates pin in pool mode, "" to derive the rev from
   * the customer checkout's git history instead. Always a full 40-hex sha
   * when set: the platform's template cache is keyed by exact rev, and a
   * mutable ref re-resolves. */
  readonly templateRev: string
}

const SPEC_KEYS: Record<string, "string" | "int" | "regions"> = {
  "pool-name": "string",
  "template-attr": "string",
  "flake-dir": "string",
  region: "string",
  regions: "regions",
  "runner-label": "string",
  "max-runners": "int",
  headroom: "int",
  "min-warm": "int",
  "idle-grace-seconds": "int",
  "max-cold-boots": "int",
}

export const DEFAULT_SPEC_PATH = ".github/ix-runners.toml"

/** Event names on which retiring capacity is allowed. An event tick fires
 * when a run is REQUESTED - before its jobs are queued - so it sees an idle
 * pool at the moment a wave is landing, and may only ever add. */
const SCALE_DOWN_EVENTS = new Set(["", "schedule", "workflow_dispatch"])

export async function loadConfig(): Promise<Config> {
  const path = process.env.IX_POOL_SPEC
  const spec = path ? await loadSpec(path) : specFromEnv()
  return fromSpec(spec)
}

async function loadSpec(path: string): Promise<Record<string, unknown>> {
  const file = Bun.file(path)
  if (!(await file.exists())) {
    // The action always sets IX_POOL_SPEC, so an absent file at the DEFAULT
    // path is the documented "every key has a default" case, not an error.
    // Only an explicitly named file that is missing is broken intent.
    if (path === DEFAULT_SPEC_PATH) return {}
    logError(
      `no pool spec at ${path}. A minimal one is one line: region = "us-west-1".`,
    )
    process.exit(1)
  }
  let spec: Record<string, unknown>
  try {
    spec = Bun.TOML.parse(await file.text()) as Record<string, unknown>
  } catch (error) {
    logError(`${path} could not be read as TOML: ${error}`)
    process.exit(1)
  }
  const problems: string[] = []
  for (const [key, value] of Object.entries(spec)) {
    const want = SPEC_KEYS[key]
    if (want === undefined) problems.push(`unknown key '${key}'`)
    else if (want === "int" && (typeof value !== "number" || !Number.isInteger(value)))
      problems.push(`'${key}' must be a whole number, got ${JSON.stringify(value)}`)
    else if (want === "string" && typeof value !== "string")
      problems.push(`'${key}' must be a string, got ${JSON.stringify(value)}`)
    else if (
      want === "regions" &&
      (!Array.isArray(value) || value.length === 0 || value.some((v) => typeof v !== "string" || !v))
    )
      problems.push(`'${key}' must be a non-empty list of region strings`)
  }
  if (problems.length > 0) {
    for (const problem of problems) logError(`${path}: ${problem}`)
    logError(`${path}: known keys are ${Object.keys(SPEC_KEYS).sort().join(", ")}`)
    process.exit(1)
  }
  return spec
}

/** The same shape, assembled from the environment: what fills the gaps in
 * tests, where the ambient half (repo, event) already comes from env. */
function specFromEnv(): Record<string, unknown> {
  const spec: Record<string, unknown> = {}
  for (const [key, want] of Object.entries(SPEC_KEYS)) {
    const raw = process.env[key.toUpperCase().replaceAll("-", "_")]
    if (!raw) continue
    if (want === "int") spec[key] = Number(raw)
    else if (want === "regions") spec[key] = raw.split(",").map((s) => s.trim()).filter(Boolean)
    else spec[key] = raw
  }
  return spec
}

function fromSpec(spec: Record<string, unknown>): Config {
  const text = (key: string, fallback: string) =>
    typeof spec[key] === "string" ? (spec[key] as string) : fallback
  const int = (key: string, fallback: number) =>
    typeof spec[key] === "number" ? (spec[key] as number) : fallback

  if (spec.region !== undefined && spec.regions !== undefined) {
    logError(
      "the pool spec sets both `region` and `regions`; set exactly one" +
        " (`regions` with a single entry is the same pool as `region`)",
    )
    process.exit(1)
  }
  const regions = Array.isArray(spec.regions)
    ? (spec.regions as string[])
    : [text("region", "us-west-1")]
  if (new Set(regions).size !== regions.length) {
    logError(`\`regions\` repeats an entry (${JSON.stringify(regions)}); each appears once`)
    process.exit(1)
  }

  let flakeDir = text("flake-dir", "").trim().replace(/^\.\//, "").replace(/\/+$/, "")
  if (flakeDir === ".") flakeDir = ""
  if (flakeDir.startsWith("/") || flakeDir.split("/").includes("..")) {
    logError(`flake-dir '${flakeDir}' must be a directory inside the repository`)
    process.exit(1)
  }

  const repo = process.env.GITHUB_REPOSITORY
  if (!repo) {
    logError("GITHUB_REPOSITORY is required")
    process.exit(1)
  }

  // -- pool mode -------------------------------------------------------------
  // Set by the action when the pool is one THIS repository ships (the
  // `pool:` input): the spec came from the action's own checkout, so the
  // templates must build from the action's repository at the action's own
  // pinned commit - not from the customer repo, whose history says nothing
  // about this pool. The rev-roll law is unchanged in shape: seeds key on
  // this rev, so bumping the `uses:` pin is what re-seeds the fleet, and a
  // customer merge never can.
  const actionRev = (process.env.IX_RUNNERS_ACTION_REV ?? "").trim()
  const actionRepo = (process.env.IX_RUNNERS_ACTION_REPO ?? "").trim()
  if (!actionRev !== !actionRepo) {
    logError(
      "IX_RUNNERS_ACTION_REV and IX_RUNNERS_ACTION_REPO must be set together" +
        ` (the action sets both under its \`pool\` input); got rev='${actionRev}',` +
        ` repo='${actionRepo}'`,
    )
    process.exit(1)
  }
  if (actionRev && !/^[0-9a-f]{40}$/.test(actionRev)) {
    logError(
      `the action ref '${actionRev}' is not a full commit sha. To use the` +
        " `pool:` input, pin the action by commit" +
        " (uses: indexable-inc/ix-runners@<40-hex sha>): seeds and the" +
        " template cache key on that exact rev, and a tag or branch both" +
        " re-resolves and defeats the action's own pin-by-commit posture.",
    )
    process.exit(1)
  }
  if (actionRev && !flakeDir) {
    // A shipped pool always lives in a subflake; this repo's root flake
    // defines the mechanism, not a bootable machine.
    logError(
      "the pool spec came from the action's own checkout but sets no" +
        ' flake-dir; a shipped pool must name its subflake (flake-dir = "pools/<name>")',
    )
    process.exit(1)
  }

  // Which trigger this is. NOT a spec key: the trigger already says, and an
  // operator pinning it in a file would pin it for the cron too. TICK_MODE
  // exists for tests.
  const tickMode =
    process.env.TICK_MODE ??
    (SCALE_DOWN_EVENTS.has(process.env.GITHUB_EVENT_NAME ?? "") ? "scheduled" : "event")
  return {
    repo,
    pool: text("pool-name", repo.split("/")[1]!.toLowerCase()),
    templateAttr: text("template-attr", "ci-runner"),
    flakeDir,
    regions,
    runnerLabel: text("runner-label", "ix"),
    maxRunners: int("max-runners", 16),
    headroom: int("headroom", 1),
    minWarm: int("min-warm", 0),
    idleGraceSeconds: int("idle-grace-seconds", 900),
    maxColdBoots: int("max-cold-boots", 4),
    warmGraceSeconds: 300,
    mayScaleDown: tickMode === "scheduled",
    templateRepo: actionRepo || repo,
    templateRev: actionRev,
  }
}

/** The rev this tick converges toward. In pool mode it IS the action's
 * pinned commit - the customer checkout's git history (which may not even
 * exist under `pool:`) is never consulted. */
export async function resolveRev(config: Config): Promise<string> {
  if (config.templateRev) return config.templateRev
  return desiredRev(config.flakeDir)
}

/** Last commit touching the runner config - NOT GITHUB_SHA: unrelated merges
 * must not roll the fleet, and the template cache is keyed by exact rev. */
export async function desiredRev(flakeDir: string): Promise<string> {
  const shallow = await git("rev-parse", "--is-shallow-repository")
  if (shallow === "true") {
    // A shallow checkout's grafted boundary commit diffs against the empty
    // tree, so `git log -- <paths>` names HEAD for EVERY commit and the
    // whole fleet rolls on every push - silently.
    logError(
      "the checkout is shallow, so the runner-config rev cannot be derived." +
        " Check out with fetch-depth: 0.",
    )
    process.exit(1)
  }
  const paths = flakeDir ? [flakeDir] : CONFIG_PATHS
  const rev = await git("log", "-1", "--format=%H", "--", ...paths)
  if (!/^[0-9a-f]{40}$/.test(rev)) {
    logError(`no commit touches the runner config (${paths.join(", ")})`)
    process.exit(1)
  }
  return rev
}

async function git(...args: string[]): Promise<string> {
  const proc = Bun.spawn(["git", ...args], { stdout: "pipe", stderr: "pipe" })
  const [out, code] = await Promise.all([new Response(proc.stdout).text(), proc.exited])
  if (code !== 0) {
    logError(`git ${args.join(" ")} failed (${code})`)
    process.exit(1)
  }
  return out.trim()
}
