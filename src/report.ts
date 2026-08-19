/** Everything this run says out loud: annotations, masking, the job summary.
 *
 * Remote strings reach all of it - a runner's name, an ix failure reason, an
 * error quoting either - so `clean` is not decoration: a newline opens a
 * fresh line, which is where `::` workflow commands parse and where a
 * summary table takes another row. */

const CONTROL_CHARACTERS = /[\x00-\x1f\x7f-\x9f]/g

/** One line, safe to print: a remote string cannot forge output. */
export function clean(value: unknown): string {
  return String(value).replace(CONTROL_CHARACTERS, " ")
}

/** An Actions error annotation - surfaced on the run, not buried in logs. */
export function logError(message: string): void {
  console.log(`::error::${clean(message)}`)
}

export function logWarning(message: string): void {
  console.log(`::warning::${clean(message)}`)
}

/** Redact a credential from everything this process prints from here on,
 * tracebacks included. Print it before the credential can reach any output. */
export function mask(secret: string): void {
  console.log(`::add-mask::${secret}`)
}

/** Append a per-subject outcome table to the job summary, when in Actions. */
export async function writeSummary(
  rows: readonly (readonly [string, string, string])[],
): Promise<void> {
  const path = process.env.GITHUB_STEP_SUMMARY
  if (!path || rows.length === 0) return
  const cell = (value: string) => clean(value).replaceAll("|", "\\|")
  const lines = [
    "",
    "| subject | action | outcome |",
    "| --- | --- | --- |",
    ...rows.map((row) => `| ${row.map(cell).join(" | ")} |`),
    "",
  ]
  const existing = await Bun.file(path).text().catch(() => "")
  await Bun.write(path, existing + lines.join("\n"))
}
