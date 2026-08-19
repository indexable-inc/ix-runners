/** The config seam pool mode rides: where the template repo and rev come
 * from, and the refusals that keep a mutable or half-set pin out of the
 * fleet. Exit paths are tested by mocking process.exit to throw; the
 * resolveRev bypass is tested the hard way - in a directory where any git
 * consultation would kill the run. */

import { afterEach, describe, expect, spyOn, test } from "bun:test"
import { mkdtempSync } from "node:fs"
import { tmpdir } from "node:os"
import { join } from "node:path"
import type { Config } from "./config.ts"
import { loadConfig, resolveRev } from "./config.ts"

const ACTION_REV = "4751cbbab884173b3a3bcee7c19808e89a18bb37"
const POOL_MODE = {
  FLAKE_DIR: "pools/baml",
  IX_RUNNERS_ACTION_REV: ACTION_REV,
  IX_RUNNERS_ACTION_REPO: "indexable-inc/ix-runners",
}

/** Every env key the config reads; managed as a set so one test's leftovers
 * cannot leak into the next. */
const MANAGED = [
  "GITHUB_REPOSITORY",
  "GITHUB_EVENT_NAME",
  "TICK_MODE",
  "IX_POOL_SPEC",
  "FLAKE_DIR",
  "REGION",
  "REGIONS",
  "POOL_NAME",
  "IX_RUNNERS_ACTION_REV",
  "IX_RUNNERS_ACTION_REPO",
] as const

const saved = new Map<string, string | undefined>()
function setEnv(env: Record<string, string>): void {
  for (const key of MANAGED) {
    if (!saved.has(key)) saved.set(key, process.env[key])
    delete process.env[key]
  }
  process.env.GITHUB_REPOSITORY = "example/baml"
  for (const [key, value] of Object.entries(env)) process.env[key] = value
}
afterEach(() => {
  for (const [key, value] of saved) {
    if (value === undefined) delete process.env[key]
    else process.env[key] = value
  }
  saved.clear()
})

/** loadConfig exits on a refusal; turn that into a throw the test can see. */
class Exit extends Error {}
async function refusal(env: Record<string, string>): Promise<void> {
  setEnv(env)
  const exit = spyOn(process, "exit").mockImplementation((() => {
    throw new Exit()
  }) as never)
  try {
    await expect(loadConfig()).rejects.toBeInstanceOf(Exit)
  } finally {
    exit.mockRestore()
  }
}

describe("pool mode config", () => {
  test("without the action env, templates stay on the customer repo", async () => {
    setEnv({})
    const config = await loadConfig()
    expect(config.templateRepo).toBe("example/baml")
    expect(config.templateRev).toBe("")
  })

  test("the action env pins the template repo and rev", async () => {
    setEnv(POOL_MODE)
    const config = await loadConfig()
    expect(config.templateRepo).toBe("indexable-inc/ix-runners")
    expect(config.templateRev).toBe(ACTION_REV)
    // GitHub-side identity is still the customer repository.
    expect(config.repo).toBe("example/baml")
  })

  test("a mutable action ref is refused", async () => {
    // Seeds and the template cache key on the exact rev; a tag or branch
    // re-resolves. Uppercase hex is not what GitHub emits either.
    for (const ref of ["main", "v2", "4751cbb", ACTION_REV.toUpperCase()]) {
      await refusal({ ...POOL_MODE, IX_RUNNERS_ACTION_REV: ref })
    }
  })

  test("the action env pair must arrive together", async () => {
    await refusal({ FLAKE_DIR: "pools/baml", IX_RUNNERS_ACTION_REV: ACTION_REV })
    await refusal({ FLAKE_DIR: "pools/baml", IX_RUNNERS_ACTION_REPO: "indexable-inc/ix-runners" })
  })

  test("pool mode requires a subflake", async () => {
    // This repo's root flake defines the mechanism, not a bootable machine.
    await refusal({ ...POOL_MODE, FLAKE_DIR: "" })
  })

  test("the shipped baml spec loads through the real code path", async () => {
    // pools/baml/ix-runners.toml is exactly what `pool: baml` resolves to;
    // loading it here keeps the shipped file inside the spec vocabulary -
    // an unknown key there would take down every tick of the pool at once.
    setEnv({
      ...POOL_MODE,
      FLAKE_DIR: "", // the file carries flake-dir; env must not shadow it
      IX_POOL_SPEC: join(import.meta.dir, "..", "pools", "baml", "ix-runners.toml"),
    })
    const config = await loadConfig()
    expect(config.pool).toBe("baml")
    expect(config.flakeDir).toBe("pools/baml")
    expect(config.templateAttr).toBe("ci-runner")
    expect(config.regions).toEqual(["us-west-1", "us-east-1"])
    expect(config.maxRunners).toBe(32)
    expect(config.templateRepo).toBe("indexable-inc/ix-runners")
    expect(config.templateRev).toBe(ACTION_REV)
  })

  test("resolveRev never consults git in pool mode", async () => {
    // Run in a directory that is NOT a git repository, with process.exit
    // unmocked: if the bypass is broken, desiredRev's git call fails and
    // exits this whole test run - a hard failure, not a soft assert.
    setEnv(POOL_MODE)
    const config: Config = { ...(await loadConfig()) }
    const before = process.cwd()
    process.chdir(mkdtempSync(join(tmpdir(), "ix-runners-nogit-")))
    try {
      expect(await resolveRev(config)).toBe(ACTION_REV)
    } finally {
      process.chdir(before)
    }
  })
})
