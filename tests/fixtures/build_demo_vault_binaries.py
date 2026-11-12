"""Regenerate the two binary files of the demo vault (one PDF, one DOCX) from the text below.

The committed files are the source of truth for tests; this script exists so a reviewer can see
exactly what they contain and rebuild them offline:

    python tests/fixtures/build_demo_vault_binaries.py

The PDF is written by hand (one page, the standard Helvetica font, no metadata) so no PDF library
is needed. The DOCX uses python-docx, which is already a core dependency (parsers.py). Both texts
are fictional sample data (CON-012 / UX-008).
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

VAULT = Path(__file__).resolve().parent / "demo-vault"

PDF_PATH = VAULT / "research" / "user-interviews-summary.pdf"
PDF_LINES = [
    "User interviews summary",
    "Author: Priya (design lead). Date: 2026-08-05.",
    "",
    "We interviewed 12 people who keep a journal at least once a week.",
    "Top request: shared streaks with a small private group (7 of 12 participants).",
    "Second request: full offline use (5 of 12 participants).",
    "Most common complaint: apps that nag with notifications (9 of 12 participants).",
    "Nobody asked for public leaderboards; 4 participants said they would avoid them.",
    "",
    "Recommendation: build shared streaks for groups of 2 to 8 people, private by default.",
]

DOCX_PATH = VAULT / "support" / "refund-policy.docx"
DOCX_TITLE = "Quillwend refund policy"
DOCX_PARAGRAPHS = [
    "Owner: Jonah. Version 1, 2026-09-12. Applies to every paid plan.",
    "You can ask for a refund within 30 days of any payment, monthly or annual, for any reason.",
    "Approved refunds are paid back to the original payment method within 5 business days.",
    "After 30 days an annual plan can still be cancelled; it then stays active until the end of "
    "the paid year and is not refunded.",
    "To ask for a refund, email support@nimbrel.example from the address on the account.",
]
DOCX_TABLE = [
    ("Plan", "Refund window", "Paid back within"),
    ("Solo", "30 days", "5 business days"),
    ("Team", "30 days", "5 business days"),
]


def _pdf_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def build_pdf() -> bytes:
    ops = ["BT", "/F1 11 Tf", "14 TL", "72 740 Td"]
    for line in PDF_LINES:
        ops.append(f"({_pdf_escape(line)}) Tj T*")
    ops.append("ET")
    stream = "\n".join(ops).encode("ascii")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return bytes(out)


def build_docx(path: Path) -> None:
    import docx  # python-docx

    document = docx.Document()
    props = document.core_properties
    props.author = "Nimbrel Studio (fictional sample data)"
    props.last_modified_by = props.author
    props.title = DOCX_TITLE
    props.created = props.modified = dt.datetime(2026, 9, 12, tzinfo=dt.UTC)
    props.revision = 1
    document.add_heading(DOCX_TITLE, level=1)
    for paragraph in DOCX_PARAGRAPHS:
        document.add_paragraph(paragraph)
    table = document.add_table(rows=len(DOCX_TABLE), cols=len(DOCX_TABLE[0]))
    for row, values in zip(table.rows, DOCX_TABLE, strict=True):
        for cell, value in zip(row.cells, values, strict=True):
            cell.text = value
    document.save(str(path))


def main() -> None:
    PDF_PATH.write_bytes(build_pdf())
    build_docx(DOCX_PATH)
    print(f"wrote {PDF_PATH.relative_to(VAULT.parent)} and {DOCX_PATH.relative_to(VAULT.parent)}")


if __name__ == "__main__":
    main()
