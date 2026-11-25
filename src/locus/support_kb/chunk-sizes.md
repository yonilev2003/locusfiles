---
id: chunk-sizes
title: Chunk sizes: how Locus cuts files into pieces
url: /support
keywords: chunk, chunks, chunk size, chunk sizes, tokens, 300, 700, overlap, LOCUS_CHUNK_TOKENS, LOCUS_OVERLAP_TOKENS, dry-run, chunk_index, chunk number, split, cut, passage, excerpt, window, large window, collection
updated: 2026-11-22
---
Locus cuts each file into chunks. Each search result is one chunk, and it carries the file path and the chunk number
(`chunk_index`).

- **New default collection:** 300 "tokens" per chunk. A token here is an estimate of about 4 characters, not the
  model's own tokenizer.
- **700 tokens** applies to collections made before 0.1.4, to a model with a large window (`bge-m3`) or a hosted
  provider, and when `LOCUS_CHUNK_TOKENS=700` is set.
- `locus index --dry-run` says which size a collection will use. `LOCUS_OVERLAP_TOKENS` sets the overlap.

Status: these sizes are read from the code and the README. Their effect on answer quality over your own files was
not measured (unverified).

Changing the chunk size changes the index: re-index the folder, or use a new collection (`LOCUS_COLLECTION`).
