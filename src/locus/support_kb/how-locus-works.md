---
id: how-locus-works
title: How Locus works: the architecture in one page
url: /support
keywords: architecture, how it works, pipeline, parse, chunk, embed, store, chroma, index, vector, semantic search, mcp server, stdio, locus serve, what is locus, freshness step, data folder, local-first, tools, locus_search, locus_list_sources, locus_read_file
updated: 2026-11-22
---
### What is Locus?
Locus is a local-first MCP server. It indexes folders you choose on your own computer and gives an AI assistant a search
tool over them. Every search result carries the file path and a chunk number, so the assistant can cite its source.

**The pipeline.** A folder is parsed into text, cut into chunks, turned into vectors by an embedding model, and stored
in a local Chroma database in `~/.locus` (or the folder `LOCUS_DATA_DIR` names). Locus does not copy your files there:
it stores chunk text, vectors and metadata.

**A search.** The MCP client calls `locus_search` over stdio. Locus first runs a short freshness step (2 seconds by
default) that indexes new and changed files, embeds the query with the same model as the index, finds the nearest
chunks, and returns them with the path and chunk number. The client sends those excerpts to its own model provider.

**Tools.** Five are always listed: `locus_search`, `locus_list_sources`, `locus_read_file`, `locus_help` and
`locus_diagnose_connection`. `locus_read_file` reads only paths that are already in the index. A sixth,
`locus_account_status`, exists only when `LOCUS_API_KEY` is set.

**Three ways in.** MCP over stdio, a command line with `--json`, and a local HTTP API.

**Keeping the index current.** Re-run `locus index <folder> --prune` after changes, or keep `locus watch <folder>`
running. Unchanged files are skipped; `--prune` also removes entries for files that no longer exist.

**One model per collection.** The embedder name is stored in the collection. Switching models means a new collection
(`LOCUS_COLLECTION`), never a mixed one.

See also: the articles on privacy, file types, chunk sizes and models.
