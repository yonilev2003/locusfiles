"""Minimal PDFs for tests, written by hand (standard Helvetica, one text stream per page): no PDF writer is a
dependency. `make_pdf([["line", "line"], [], ["page three"]])` gives a 3-page file; an empty page list is a blank
page, which is what a scanned page looks like to a text extractor (the image is not text)."""

from __future__ import annotations

from pathlib import Path


def _escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def make_pdf(pages: list[list[str]]) -> bytes:
    n = len(pages)
    page_ids = [3 + 2 * i for i in range(n)]  # page object ids; each page's content stream is the next id
    font_id = 3 + 2 * n
    objects: list[bytes] = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        ("<< /Type /Pages /Kids [" + " ".join(f"{p} 0 R" for p in page_ids) + f"] /Count {n} >>").encode(),
    ]
    for pid, lines in zip(page_ids, pages, strict=True):
        ops = ["BT", "/F1 11 Tf", "14 TL", "72 740 Td"] + [f"({_escape(ln)}) Tj T*" for ln in lines] + ["ET"]
        stream = "\n".join(ops).encode("latin-1")
        objects.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 {font_id} 0 R >> >> "
            f"/Contents {pid + 1} 0 R >>".encode()
        )
        objects.append(b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream")
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>")
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


def write_pdf(path: Path, pages: list[list[str]]) -> Path:
    path.write_bytes(make_pdf(pages))
    return path
