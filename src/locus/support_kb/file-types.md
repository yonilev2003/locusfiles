---
id: file-types
title: File types Locus reads, and what it skips
url: /support
keywords: file types, formats, supported formats, pdf, docx, pptx, xlsx, ods, odt, rtf, epub, ipynb, markdown, text, code, csv, html, subtitles, srt, scanned, scan, ocr, images, hidden files, size cap, LOCUS_MAX_FILE_MB, LOCUS_EXTENSIONS, extension, skipped, not indexed
updated: 2026-11-22
---
**Read:** Markdown, text, code, CSV, HTML, PDF with real text, DOCX, PPTX, XLSX, ODS, ODT, RTF, EPUB, IPYNB and
subtitle files.

**Not read:**

- Scans, photos and handwriting: there is no text recognition. A PDF with no text layer has nothing to index, and
  images are not indexed.
- Hidden files and folders (such as `.env` and `.git`).
- Credentials and key material, refused by name and place (passwords, tokens, keys, `~/.ssh`, keychains, browser
  profiles).
- Files over the size cap (25 MB by default, `LOCUS_MAX_FILE_MB`).
- Anything that is not a regular file, and a file with more than one hard link unless `LOCUS_ALLOW_HARD_LINKS=1`.

`LOCUS_EXTENSIONS` changes the list of extensions. Formulas and tables in PDFs can come out scrambled (seen in spot
checks, not measured).
