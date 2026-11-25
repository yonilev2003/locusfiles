---
id: troubleshooting-recipes
title: Troubleshooting: six recipes for the most common problems
url: /support
keywords: troubleshooting, troubleshoot, problem, not working, broken, fix, recipe, recipes, doctor, locus doctor, locus stats, locus_diagnose_connection, assistant does not use locus, no results, empty, stale, slow, model download, offline, cannot connect, mcp, restart, different settings
updated: 2026-11-22
---
1. **Install or first run fails.** Run `locus doctor`; each FAIL line names the check. The first run needs to download
   the 134 MB model. See the article on common errors.
2. **The AI app does not see Locus.** Restart the app after adding Locus; check the `locus serve` command in its MCP
   settings is the full path (`command -v locus`). In Claude Code, `/mcp` should show `locus` as Connected.
3. **The assistant answers without searching.** Say "search my files with Locus". Assistants do not always call it.
4. **No results.** Run `locus stats`. If it is empty, run `locus index <folder> --prune`. The app may use other
   settings (`LOCUS_COLLECTION`, `LOCUS_DATA_DIR`, `LOCUS_EMBEDDER`): ask it to run `locus_diagnose_connection`.
5. **A file is missing.** Check its type and size (file-types article), that it is not in `~/Downloads` (closed until
   you open it) and not covered by a folder rule or the deny-list.
6. **Results look old.** Run `locus index <folder> --prune`, or keep `locus watch <folder>` running. If paused,
   `locus resume`.
