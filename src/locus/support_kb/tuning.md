---
id: tuning
title: Tuning: settings that change what Locus finds and how fast
url: /support
keywords: tuning, tune, settings, configure, configuration, environment variables, LOCUS_CHUNK_TOKENS, LOCUS_OVERLAP_TOKENS, LOCUS_LOCAL_MODEL, LOCUS_MAX_FILE_MB, LOCUS_EXTENSIONS, LOCUS_COLLECTION, LOCUS_DATA_DIR, top_k, path_prefix, date filters, modified_after, faster, better results, improve results, prune, watch, estimate
updated: 2026-11-22
---
Start with the defaults. Change one setting at a time, and re-index after changing a model or a chunk setting.

- **Narrow the search before raising `top_k`:** use `path_prefix` or the date filters (`modified_after`,
  `modified_before`). A higher `top_k` adds paths to check, not text.
- **Keep the index current:** `locus index <folder> --prune`, or `locus watch <folder>`.
- **Index less:** start with a small folder; `locus estimate <folder>` shows the size first.
- **Chunk size:** `LOCUS_CHUNK_TOKENS`, `LOCUS_OVERLAP_TOKENS`; see the chunk-size article.
- **Model:** `LOCUS_LOCAL_MODEL`, or `LOCUS_EMBEDDER` for your own provider; one model per collection.
- **Which files:** `LOCUS_EXTENSIONS`, `LOCUS_MAX_FILE_MB`, and folder rules in `~/.locus/policy.toml`.
- **Separate indexes:** `LOCUS_COLLECTION`, `LOCUS_DATA_DIR`.

Whether a setting improves results on your own files was not measured; compare on questions you know the answer to.
