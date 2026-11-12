"""Rules-only classification (draft ADR-082 item 4): extension, optional UTI, a few fixed name patterns.

`classify` returns one value of `labels.LABELS` for any input, including hostile names; it reads nothing
but the name's suffix and the screenshot pattern, and never the file's contents."""

from __future__ import annotations

import re
import shutil
import subprocess
from collections.abc import Callable
from pathlib import PurePath

from .labels import LABELS

EXTENSIONS: dict[str, str] = {}
for _label, _exts in {
    "documents": "pdf doc docx odt rtf txt md markdown pages tex",
    "spreadsheets": "xls xlsx ods csv tsv numbers",
    "presentations": "ppt pptx odp key",
    "images": "jpg jpeg png gif webp heic heif tiff tif bmp svg raw cr2 nef",
    "audio": "mp3 m4a wav flac aac ogg aiff",
    "video": "mp4 mov mkv avi webm m4v",
    "archives": "zip tar gz tgz bz2 xz 7z rar",
    "installers": "dmg pkg exe msi deb",
    "code": "py js ts tsx jsx json yaml yml toml sh rb go rs java c cpp h swift ipynb",
    "books": "epub mobi azw3 djvu",
}.items():
    for _ext in _exts.split():
        EXTENSIONS[_ext] = _label

# macOS names screenshots "Screenshot 2026-10-23 at 14.03.11.png" / "Screen Shot ...": a fixed pattern.
SCREENSHOT = re.compile(r"^(Screenshot|Screen Shot|Screen Recording) \d{4}-\d{2}-\d{2} at \d{1,2}\.\d{2}\.\d{2}")

# Content types from `mdls -name kMDItemContentTypeTree`; used only when the extension gave no label.
UTI_LABELS: tuple[tuple[str, str], ...] = (
    ("public.image", "images"),
    ("public.audio", "audio"),
    ("public.movie", "video"),
    ("public.archive", "archives"),
    ("com.adobe.pdf", "documents"),
    ("public.spreadsheet", "spreadsheets"),
    ("public.presentation", "presentations"),
    ("public.source-code", "code"),
    ("public.text", "documents"),
)

UtiLookup = Callable[[str], tuple[str, ...]]


def mdls_content_types(path: str) -> tuple[str, ...]:
    """The file's UTI tree from Spotlight (`mdls`), or () when unavailable. Reads metadata only, never contents.
    The path is passed as an argument vector element, never through a shell."""
    exe = shutil.which("mdls")
    if exe is None:
        return ()
    try:
        out = subprocess.run(
            [exe, "-raw", "-name", "kMDItemContentTypeTree", "--", path],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return ()
    return tuple(re.findall(r'"([A-Za-z0-9.\-]+)"', out))


def classify(name: str, *, path: str | None = None, uti_lookup: UtiLookup | None = None) -> str:
    """The label for a file name. `uti_lookup(path)` is consulted only when the extension is unknown."""
    suffix = PurePath(name).suffix.lower().lstrip(".")
    if SCREENSHOT.match(name) and suffix in ("png", "jpg", "jpeg", "heic", "mov"):
        return "screenshots"
    label = EXTENSIONS.get(suffix)
    if label is None and uti_lookup is not None and path is not None:
        tree = uti_lookup(path)
        for prefix, candidate in UTI_LABELS:
            if prefix in tree:
                label = candidate
                break
    label = label or "other"
    assert label in LABELS
    return label
