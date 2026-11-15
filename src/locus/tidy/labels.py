"""The fixed label enum and the folder each label maps to.

Labels are the only thing a classifier may output; folders are the only place a move may go. Both are
constants here, not read from a file name, a file's contents or a model answer."""

from __future__ import annotations

LABELS: tuple[str, ...] = (
    "documents",
    "spreadsheets",
    "presentations",
    "images",
    "screenshots",
    "audio",
    "video",
    "archives",
    "installers",
    "code",
    "books",
    "other",
)

# "other" has no folder: such a file stays where it is and is listed in the plan's `kept` section.
FOLDERS: dict[str, str | None] = {
    "documents": "Documents",
    "spreadsheets": "Spreadsheets",
    "presentations": "Presentations",
    "images": "Images",
    "screenshots": "Screenshots",
    "audio": "Audio",
    "video": "Video",
    "archives": "Archives",
    "installers": "Installers",
    "code": "Code",
    "books": "Books",
    "other": None,
}

# Labels whose files are filed into a YYYY-MM subfolder by their modified time.
MONTH_BUCKETS: frozenset[str] = frozenset({"screenshots", "images"})

assert set(FOLDERS) == set(LABELS)
