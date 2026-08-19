import { describe, expect, test } from "bun:test"
import { HttpError, trustedRunProvenance } from "./github.ts"
import { clean } from "./report.ts"

describe("run provenance gates seed candidacy", () => {
  const repo = "acme/app"

  test("a FORK pull request whose head branch is named 'main' is NOT trusted", () => {
    // The security case: head_branch === defaultBranch matches, so
    // provenance is the only thing between fork code and the seed every
    // trusted job forks from.
    const run = { event: "pull_request", head_repository: { full_name: "mallory/app" } }
    expect(trustedRunProvenance(run, repo)).toBe(false)
  })

  test("a push run is trusted", () => {
    expect(trustedRunProvenance({ event: "push" }, repo)).toBe(true)
  })

  test("a same-repository run of any event is trusted", () => {
    const run = { event: "schedule", head_repository: { full_name: "acme/app" } }
    expect(trustedRunProvenance(run, repo)).toBe(true)
  })

  test("repository names compare case-insensitively, as GitHub treats them", () => {
    const run = { event: "workflow_dispatch", head_repository: { full_name: "Acme/App" } }
    expect(trustedRunProvenance(run, repo)).toBe(true)
  })

  test("a run with no head repository at all is not trusted", () => {
    expect(trustedRunProvenance({ event: "pull_request" }, repo)).toBe(false)
  })
})

describe("the 422 busy classification", () => {
  test("a busy runner's refusal reads as busy", () => {
    const error = new HttpError(422, '{"message":"Runner \\"x\\" is busy and cannot be deleted"}', "/actions/runners/1")
    expect(error.saysBusy()).toBe(true)
  })
  test("a rate-limit 422 does not", () => {
    const error = new HttpError(422, '{"message":"Validation failed: abuse detection"}', "/actions/runners/1")
    expect(error.saysBusy()).toBe(false)
  })
  test("the secret-tainted body never rides the message", () => {
    const error = new HttpError(422, "token ghs_secret123 leaked here", "/actions/runners/1")
    expect(error.message).not.toContain("ghs_secret123")
  })
})

describe("clean strips what GitHub's log renderer would obey", () => {
  test("control characters are blanked, never forwarded", () => {
    const cleaned = clean("evil\x1b[31m\r\ntext\x07")
    expect(cleaned).toBe("evil [31m  text ")
    expect(cleaned).not.toMatch(/[\x00-\x1f\x7f]/)
  })
  test("plain text passes through", () => {
    expect(clean("a normal message. rc=1")).toBe("a normal message. rc=1")
  })
})
