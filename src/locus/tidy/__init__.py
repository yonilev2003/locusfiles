"""Downloads assistant core, rules only (draft ADR-082, night 1 of keep-and-grow, 2026-10-24).

Plan -> approve -> execute -> undo for moving top-level files into fixed folders. Library code only:
no command, no MCP tool and no model is wired to it, and nothing here is reachable from `locus serve`.
The six MCP tools stay read-only.

What it guarantees, each pinned by tests/test_tidy.py on fixture folders:
- The label of a file comes from its extension, an optional UTI and a few fixed name patterns, and is
  always one value of `labels.LABELS`. A file name is data: it is never read as an instruction and never
  becomes part of a destination path except as the file's own final name.
- A destination is `<root>/<fixed folder for the label>[/<YYYY-MM>]/<the file's own name>`, nothing else.
- Move-only: no delete, no overwrite, no recursion, top-level regular files only. A move uses a rename that
  fails if the destination exists (RENAME_EXCL on macOS, RENAME_NOREPLACE on Linux, hard link + unlink as
  a last resort), so a collision is an error, not a replacement. Quarantine and Finder tags travel with a
  same-volume rename; nothing here clears or reads them.
- Unstable or unsafe files are skipped with a reason (partial downloads, young or still-growing files,
  symlinks, hidden files, hard links, cloud placeholders, directories and packages).
- A write-ahead journal records `intent` before and `done` after every move; `recover` finishes or rolls back
  an interrupted run; `undo` replays it in reverse and reports each row it cannot restore.
"""

from __future__ import annotations

from .execute import ExecuteResult, UndoResult, execute_plan, recover, undo
from .labels import FOLDERS, LABELS
from .plan import Plan, Row, build_plan, load_plan, save_plan

__all__ = [
    "FOLDERS",
    "LABELS",
    "ExecuteResult",
    "Plan",
    "Row",
    "UndoResult",
    "build_plan",
    "execute_plan",
    "load_plan",
    "recover",
    "save_plan",
    "undo",
]
