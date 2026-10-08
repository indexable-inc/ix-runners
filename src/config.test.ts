/** The config seam the image rides: the `image` key, the refusals that keep a
 * mutable or missing reference out of the fleet, and the identity seeds key
 * on. Exit paths are tested by mocking process.exit to throw. */

import { afterEach, describe, expect, spyOn, test } from "bun:test"
import { join } from "node:path"
import { imageReferenceProblem, loadConfig, resolveRev } from "./config.ts"

const IMAGE = "ix/runner:2026-10-08"

/** Every env key the config reads; managed as a set so one test's leftovers
 * cannot leak into the next. */
const MANAGED = [
  "GITHUB_REPOSITORY",
  "GITHUB_EVENT_NAME",
  "TICK_MODE",
  "IX_POOL_SPEC",
  "IMAGE",
  "REGION",
  "REGIONS",
  "POOL_NAME",
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

describe("image config", () => {
  test("the image comes from the spec and nothing else", async () => {
    setEnv({ IMAGE })
    const config = await loadConfig()
    expect(config.image).toBe(IMAGE)
    // GitHub-side identity is still the customer repository.
    expect(config.repo).toBe("example/baml")
  })

  test("a missing, untagged or mutable image is refused", async () => {
    await refusal({})
    for (const image of ["ix/runner", "ix/runner:latest", "ix/runner@sha256:abc", "ix/runner :1"]) {
      await refusal({ IMAGE: image })
    }
  })

  test("tags and full digests are accepted", () => {
    expect(imageReferenceProblem(IMAGE)).toBe("")
    expect(imageReferenceProblem("registry.ix.dev:443/ix/runner:2026-10-08")).toBe("")
    expect(imageReferenceProblem(`ix/runner@sha256:${"a".repeat(64)}`)).toBe("")
  })

  test("the shipped baml spec loads through the real code path", async () => {
    // pools/baml/ix-runners.toml is exactly what `pool: baml` resolves to;
    // loading it here keeps the shipped file inside the spec vocabulary -
    // an unknown key there would take down every tick of the pool at once.
    setEnv({ IX_POOL_SPEC: join(import.meta.dir, "..", "pools", "baml", "ix-runners.toml") })
    const config = await loadConfig()
    expect(config.pool).toBe("baml")
    expect(config.image).toMatch(/^ix\/runner:\d{4}-\d{2}-\d{2}$/)
    expect(config.regions).toEqual(["us-west-1"])
    expect(config.maxRunners).toBe(32)
  })

  test("the seed identity is a pure function of the image reference", async () => {
    setEnv({ IMAGE })
    const config = await loadConfig()
    const rev = await resolveRev(config)
    expect(rev).toMatch(/^[0-9a-f]{64}$/)
    expect(await resolveRev({ ...config })).toBe(rev)
    expect(await resolveRev({ ...config, image: "ix/runner:2026-10-14" })).not.toBe(rev)
  })
})
