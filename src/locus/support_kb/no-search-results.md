---
id: no-search-results
title: Search returns no results, or not the file I expected
url: /support
keywords: no results, empty, nothing found, not found, missing file, wrong results, bad results, irrelevant, outdated, stale, stale index, old version, index empty, empty index, nothing indexed yet, not_indexed, index state, ready, collection, not indexed, file not indexed, search quality, recall, cannot find, no access, permission denied, cannot open its index, index folder, LOCUS_DATA_DIR, privacy, downloads, closed folder, hidden by your folder rules, paused, locus resume, policy.toml
updated: 2026-11-17
---
When Locus cannot search, it says why and that nothing was searched, so it is not the same as
"nothing matched". What each message means:

- **"Locus has nothing indexed yet (empty index)":** the collection the search uses is empty. Run
  `locus index <folder> --prune`, then ask again. If `locus stats` shows files, the AI app runs
  Locus with other settings (below).
- **"No results inside" a folder:** nothing in that folder is indexed. Check the path with
  `locus_list_sources`, or search without `path_prefix`.
- **"Locus cannot open its index: no access to its index folder":** the app that runs Locus may not
  open the folder `LOCUS_DATA_DIR` names (`~/.locus` by default). On macOS, allow that app in System
  Settings > Privacy & Security, or point `LOCUS_DATA_DIR` at a folder it can use, then restart the
  app. `locus_read_file` says "no access" in the same way for a single file it may not read.
- **"Locus could not search: no access":** only with the earlier hosted relay turned on (off by default):
  the API key was not accepted, or the account is not active (`locus_account_status`, present when a key
  is set, shows its state).
- **"hidden by your folder rules" or "is in a closed folder":** the file is in the index, but your folder rules keep it back:
  a closed folder you have not opened (Downloads), a folder you excluded in `~/.locus/policy.toml`, or the
  built-in deny-list (keys, passwords, keychains, browser profiles, `~/Library`, package caches). Only counts are
  shown, never names. To
  open Downloads, run `locus index ~/Downloads` in a terminal (it asks once). To change any other rule, edit
  `~/.locus/policy.toml`; the change applies at once, with no re-index. `locus doctor` lists the rules.
- **"Locus is paused by the user":** `locus pause` (or `LOCUS_DISABLED=1`) is on, so the file tools return
  nothing, not even a path. `locus resume` takes effect at once. If `LOCUS_DISABLED` is set in the app's
  settings, unset it and restart the app (a running app keeps the environment it was started with).
- **"Locus cannot read its folder rules (policy.toml)":** the file has a mistake, and nothing is returned until it is
  fixed or removed. The message names the key. `locus doctor` shows it too.
- **"Stale index":** some results come from files that changed or were deleted after they were
  indexed; the line names them, and their text may be old. Run `locus index <folder> --prune`, or
  keep `locus watch <folder>` running.

`locus_diagnose_connection` shows the index state: `not_indexed` (nothing indexed yet), `stale` (how
many indexed files changed or are gone) or `ready`, and when the index last changed.

**No results at all:**

- Run `locus stats` to see what is indexed. If it is empty, run `locus index <folder> --prune`.
- The AI app may be running Locus with different settings from your terminal (a different
  `LOCUS_COLLECTION`, `LOCUS_DATA_DIR` or `LOCUS_EMBEDDER`). Put the same values in the app's `env`
  block for the Locus server, or ask the app to run `locus_diagnose_connection`, which shows the
  collection and how many chunks it holds.

**A file is missing from results:**

- If it is in Downloads, that folder is closed until you open it (`locus index ~/Downloads`). A run over a parent
  folder such as `locus index ~` says how many files it left out.

- Check that its format is supported (text and code files, Markdown, PDF, `.docx`; and, in engine
  0.1.2 and later, `.pptx`, `.xlsx`, OpenDocument, `.epub`, `.rtf` and `.ipynb`) and that it is under
  the size cap (25 MB by default, `LOCUS_MAX_FILE_MB`). Scanned PDFs without a text layer have no
  text to index, and password-protected files cannot be read.
- Files changed since the last run are normally picked up by the next search: `locus_search` and
  `locus_list_sources` run a short freshness step (2 seconds by default). On a very large folder it can
  take several searches to catch up; run `locus index <folder> --prune`, or keep `locus watch <folder>`
  running.
- Ask with different wording, or ask the assistant to search with `path_prefix` set to the folder.
  For "list every..." questions, the assistant should check the file list first: one search returns
  only the top matches.

**Results look stale** without a "Stale index" line (for example a file added to the folder since):
re-run `locus index <folder> --prune` so new and changed files are embedded and deleted ones are
removed.
