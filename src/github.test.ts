import { describe, expect, test } from "bun:test"
import { HttpError } from "./github.ts"
import { clean } from "./report.ts"

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
