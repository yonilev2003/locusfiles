---
id: privacy-in-brief
title: Privacy in brief: what stays on your computer and what goes to your assistant
url: /support
keywords: privacy, private, upload, uploaded, upload my files, data, leaves, leave, send, sent, excerpts, assistant, provider, telemetry, pause, locus pause, audit log, downloads, deny-list, credentials, encrypted, index readable, local-first, hugging face
updated: 2026-11-22
---
### Does Locus upload my files?
No. Raw files are not uploaded. Indexing and embeddings run on your computer by default; the first run downloads the
embedding model from Hugging Face. The excerpts your assistant retrieves go to the assistant you chose, like text you
pasted in. The full list is in What leaves your computer.

**For the complete workflow, retrieved excerpts leave your computer.** They go to the assistant you chose and on to its
provider. Indexing and embeddings can stay local; this part cannot.

**Controls.**

- `~/Downloads` is closed until you open it on purpose.
- `locus pause` stops the file tools at once; `locus resume` undoes it.
- An audit log records paths and counts, never file text.
- Credentials-like files are refused by name and place.

**On disk.** The index is readable text, not encrypted. Locus does not copy your files into `~/.locus`.

**With your own provider key** (`voyage`, `openai`), chunk text goes to that provider when you index and each query
goes the same way.

The per-mode list is in the article "What leaves your computer".
