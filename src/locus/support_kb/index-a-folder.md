---
id: index-a-folder
title: Index a folder and run your first search
url: /docs/connect
keywords: index, indexing, reindex, re-index, folder, folders, add folder, prune, query, search, stats, watch, file types, formats, pdf, docx, word, pptx, powerpoint, slides, xlsx, excel, spreadsheet, odt, ods, odp, opendocument, libreoffice, epub, ebook, rtf, ipynb, jupyter, notebook, password protected, markdown, txt, code, size, notion, estimate, dry run, first search, supported formats, supported files, file formats, where is my index, where stored, data directory, LOCUS_DATA_DIR, .locus
updated: 2026-11-14
---
Point Locus at a folder, then check that search works before you connect an AI app:

```bash
locus index ~/Documents/notes --prune         # parse, chunk, embed and index the folder
locus query "what did I decide about pricing" -k 5
locus stats                                   # what is indexed right now
```

`locus query` prints matching excerpts with their file paths. If it does, indexing and search both
work, and connecting an AI app is the only step left.

- Re-run `locus index` any time: unchanged files are skipped, and `--prune` removes the index entries
  of files that no longer exist on disk or that the deny-list now refuses. To add another folder,
  run `locus index` on it too.
- A built-in deny-list refuses credentials and key material by name and place (files named like
  passwords, tokens or secrets, `.pem` and `.key` files, `~/.ssh`, browser profiles, `~/Library`
  and similar). `locus index <folder>`, `locus watch` and `--prune` apply it; `locus sync-notion`
  does not. It looks at names, not file contents.
- `locus watch <folder>` indexes a folder and then re-indexes it whenever its files change.
- `locus estimate <folder>` (or `locus index --dry-run`) shows what would be indexed and sent before
  anything is embedded. With a hosted embedder, `locus index` shows what will be sent and where,
  and asks first (`--yes` skips the question).
- File types: plain text and code files, Markdown, PDF and `.docx`. Also read (engine 0.1.2 and
  later): PowerPoint `.pptx` (slide text and speaker notes), Excel `.xlsx` (each sheet, up to 5,000
  rows and 200 columns; a whole workbook up to 200,000 characters unless you raise
  `LOCUS_SPREADSHEET_MAX_CHARS`, or set it to 0 for no limit), OpenDocument `.odt`, `.ods` and `.odp`, `.epub`, `.rtf`, Jupyter `.ipynb`
  (Markdown and code cells, not outputs), and `.markdown`, `.mdx`, `.org`, `.tex` and `.tsv` text
  files. Other formats are skipped, and so are password-protected files (`locus index` reports them
  as errors and goes on). Files over 25 MB are skipped by default (`LOCUS_MAX_FILE_MB`). The
  compatibility page lists every format.
- Locus keeps chunk text, vectors and metadata in `~/.locus`, not copies of your files. The one
  exception is Notion: `locus sync-notion` saves each page it pulls as a Markdown file there.
- Notion: `locus sync-notion` pulls and indexes Notion pages through your own Notion integration.
- Do not point Locus at your home folder or the whole machine. A full home directory is tens of thousands of
  files and can take hours to index (HYPOTHESIS for your machine: the figure depends on file count
  and embedder). Index the specific folder you need, or only recent work, and add more later. If an
  assistant suggests indexing everything, point it at one folder instead. Files outside the folders
  you indexed are not searchable until indexed.
