"""Everything this run says out loud: annotations, masking, the job summary.

Remote strings reach all of it - a runner's name, an ix failure reason, an
error quoting either - so `clean` is not decoration. A newline opens a fresh
line, which is where `::` workflow commands parse and where a summary table
takes another row.
"""

from __future__ import annotations

import os
import re
from typing import Any


# A newline in a log line opens a fresh line, where `::` workflow commands
# parse and where a summary table takes another row. Plenty of what we print
# is chosen remotely: a runner's name, an ix failure_reason or status, an
# error message quoting either.
CONTROL_CHARACTERS = re.compile(r"[\x00-\x1f\x7f-\x9f]")

def clean(value: Any) -> str:
    """One line, safe to print: a remote string cannot forge output."""
    return CONTROL_CHARACTERS.sub(" ", str(value))


def log_error(message: str) -> None:
    """An Actions error annotation - surfaced on the run, not buried in logs."""
    print(f"::error::{clean(message)}")


def log_warning(message: str) -> None:
    """An Actions warning annotation."""
    print(f"::warning::{clean(message)}")


def write_summary(rows: list[tuple[str, str, str]]) -> None:
    """Append a per-member outcome table to the job summary, when in Actions."""
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path or not rows:
        return
    lines = ["", "| member | action | outcome |", "| --- | --- | --- |"]
    # A cell carries remote text: a pipe would break the table open and a
    # newline would forge a whole row of it.
    lines += [
        "| " + " | ".join(clean(cell).replace("|", "\\|") for cell in row) + " |"
        for row in rows
    ]
    with open(path, "a", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")
