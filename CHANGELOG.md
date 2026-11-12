# Changelog

All notable changes to the Locus engine, published on PyPI as `locusfiles` (the command it installs
is `locus`). Versions follow [Semantic Versioning](https://semver.org/); while the major version is
0, a minor release may change behaviour and says so here. The version has one source:
`__version__` in `src/locus/__init__.py`.

## [0.2.0] - unreleased

The release date is added when the `v0.2.0` tag publishes it. The 0.1.3 and 0.1.4 sections below are
still marked unreleased; if they are not published first, their changes ship in 0.2.0 as well. Update
an existing install with `uv tool upgrade locusfiles` (or `pipx upgrade locusfiles`), then restart your
AI app.

**Migration:** existing collections keep the same embedder name (`local:BAAI/bge-small-en-v1.5`), so no
re-index is needed; the vectors from the two routes were measured as equivalent on the sample below.

### Changed

- **The `local` extra now runs on ONNX Runtime (through `fastembed`) instead of PyTorch.** Measured on
  one Mac (Apple silicon, fast network) on 2026-11-12, same model `BAAI/bge-small-en-v1.5`, one run each:

  | | before (PyTorch) | now (ONNX Runtime) |
  |---|---|---|
  | whole environment (`locusfiles[local]`) | 1.1 GB, plus a 133 MB model | 399 MB, plus a 64 MB model |
  | install, cold package cache | 58 s | about 9 s |
  | first `locus doctor`, with the model download | 54 s | about 20 s |
  | first index of the 21-file demo folder | 26 s | about 3.7 s |
  | each `locus query` from the CLI | about 18.5 s (the model reloads per command) | about 2.6 s |

  Parity of the two routes on 60 real passages and 22 queries (public documents, each document's own
  title as the query): cosine similarity within 2.3e-6 of 1 for every passage (worst angle 0.12 degrees), top-5 overlap 1.000, file-level recall@5 18 of 22
  for both. This is a small sample and a check that the two routes agree, not a quality claim.
- **New extra `local-torch`** keeps the PyTorch route (`sentence-transformers`) for models the default
  route does not offer: `bge-m3`, `multilingual-e5` and model folders. `LOCUS_LOCAL_BACKEND` (`auto`,
  `onnx` or `torch`) overrides the choice. The error for a missing dependency names `local-torch` when
  your model needs it.
- **ONNX models are cached in `~/.cache/locus/onnx`** (`FASTEMBED_CACHE_PATH` overrides it), because
  `fastembed`'s own default is the system temp folder, which a restart empties.

### Added

- **`locusfiles` is now also a command name** (an alias of `locus`), so `uvx --from "locusfiles[local]"
  locusfiles serve` works and MCP registry clients that run a package by its PyPI name find a command.
- **A one-line notice before the first model download:** `Locus: downloading the embedding model <name>
  (first run only) ...` on stderr, so a first run does not look hung.
- **`server.json` for the official MCP Registry** (`io.github.yonilev2003/locusfiles`), and an
  `mcp-name` line in the PyPI page for the registry's ownership check. The repository URL in it is a
  placeholder until the public repository exists.

### Fixed

- **A first-run model download could hang with no output (Hugging Face's Xet transfer path).** Plain
  HTTP is now the default (`HF_HUB_DISABLE_XET=1` is set unless you set it yourself): on a clean machine
  the Xet path stalled for over ten minutes where plain HTTP fetched the 133 MB model in 40 s.
- **`locus doctor` and `locus setup --local` recognise `fastembed`**, not only `sentence-transformers`.

### Documentation

- The README is rewritten for the public repository: a first screen that answers what it is, whether it
  works and how to try it, a runnable demo with real output, an architecture diagram with light and dark
  variants, and the same caveats as before. The PyPI page is updated for the new extras.
- `LICENSE.md` is removed; `LICENSE` is the binding text and its copyright line now sits at the top of
  `LICENSE-FAQ.md`.

## [0.1.4] - unreleased

The release date is added when the `v0.1.4` tag publishes it. Update an existing install with
`uv tool upgrade locusfiles` (or `pipx upgrade locusfiles`), then restart your AI app.

### Fixed

- **A text file with one invalid byte was indexed as garbage from start to end.** A UTF-8 file that
  failed strict decoding (one stray byte, or a character cut in half at the end) was read as
  Windows-1252 as a whole, so a Hebrew transcript came out as mojibake and could not be found by
  search. A file that is mostly valid UTF-8 (at most 5% of its non-ASCII characters invalid) is now
  read as UTF-8, with the bad bytes shown as `�`; genuine Windows-1252 files are read as before.
  Files already indexed that way are skipped by `locus index` while their content is unchanged, so
  they stay garbled until the collection is rebuilt.
- **A crash or hang on the first search right after your AI app connects (macOS, Apple Silicon).**
  The engine warms up its on-device embedder in a background thread while your AI app can already
  call `locus_search`. In 0.1.3 both could run the embedder at once; on a MacBook (M4, Apple GPU)
  that crashed the process with a segmentation fault or closed the connection. The on-device
  embedder now runs one encode at a time. On the same Mac, 110 of 110 serial runs passed with this
  change (default and Python 3.12, with and without waiting for warm-up), against 1 of 8 for 0.1.3
  when the first search came right after connecting. The first search after a fresh install still
  loads the model and can take over a minute; later searches take about 8 to 17 seconds on that Mac.

### Changed

- **New on-device collections cut files into shorter chunks.** When the on-device model reads at most 512 tokens
  (`BAAI/bge-small-en-v1.5`, `intfloat/multilingual-e5-small` and `-base`), a collection created from this version on
  uses 300-token chunks instead of 700, because the model only reads the first part of a longer chunk. On a synthetic
  Hebrew and English course, right-file-in-the-top-5 with `multilingual-e5-small` went from 0.67 to 0.82 (same-language
  questions; a synthetic test, not your files). **An index you already have does not change:** a collection built before
  this version keeps 700-token chunks for the files it indexes later. To get 300 on an existing index, index into a new
  collection (`LOCUS_COLLECTION`). `LOCUS_CHUNK_TOKENS` still overrides, and `locus index --dry-run` and
  `locus estimate` now say which chunk size the collection will use.
- **`bge-m3` no longer gets the English BGE query instruction.** Only the English `bge-small/base/large-en` models do.
  Search results for a `bge-m3` collection can change; no re-index is needed.
- **The assistant is told to search in both languages for Hebrew or mixed material** (one sentence in the server's
  guidance).

## [0.1.3] - unreleased

The release date is added when the `v0.1.3` tag publishes it. **Versions 0.1.1 and 0.1.2 were never
published:** PyPI goes from 0.1.0 straight to 0.1.3, so every change listed under 0.1.1 and 0.1.2
below first ships here, together with the changes in this section. Update an existing install with
`uv tool upgrade locusfiles` (or `pipx upgrade locusfiles`), then restart your AI app. Files of the
new types are picked up the next time you run `locus index <folder>`.

### Added

- **`locus setup --managed`: a Locus plan in one step.** It takes `LOCUS_API_KEY` if it is
  already set, or asks for it without showing what you type. It checks the key with Locus's
  account-status endpoint, which spends nothing (no embedding call), and shows your plan. Then it
  writes `LOCUS_EMBEDDER=managed` and the key into the `.env` file Locus reads in that folder: a
  new file is readable only by you, and an existing one keeps every other line. Last, it prints the
  next steps: index a folder, and the settings to give your AI app. The key is never printed. A key
  Locus does not accept, or a check that cannot reach Locus, saves nothing (`--skip-check` saves
  without checking when you are offline; `--new-key` replaces a key that is already set).
- **`locus setup --local`** does the same for on-device embeddings, with no key and no network
  call. If the `local` extra is not installed, it says how to add it.
- **No silent model switch:** if your index was built with another embedder, `locus setup` changes
  nothing and tells you to re-index into a new collection (`--collection NAME`, saved as
  `LOCUS_COLLECTION`). Your old index stays as it is.

### Changed

- **`locus_diagnose_connection` says where the relay's public key comes from.** The line
  `MCP relay configured: yes/no` is now `MCP relay public key: set by LOCUS_MCP_RELAY_SUPABASE_ANON_KEY`
  or `MCP relay public key: not set here; ...` (the relay supplies it at start). The key itself is
  never shown.
- **When to use Locus, said more concretely and more briefly.** The server instructions and the
  opening of the `locus_search`, `locus_list_sources` and `locus_read_file` descriptions now name
  the cues that mean a question is about your own material: "my", "our" or "we", one of your
  projects, clients, people or documents, or what you decided, wrote or planned. They still say what
  Locus is not for (general knowledge, current events). The instructions are shorter (1,955
  characters, down from 2,286) so that Claude Code, which keeps 2,048 characters of a server's
  instructions, no longer cuts off their last paragraph, and their first paragraph (470 characters)
  holds the whole rule. Tool descriptions are sent without the docstring indentation, the same on
  every Python version. Locus's hosted relay sends the same instructions and description openings to
  Claude.ai and ChatGPT whatever engine version you run.
- **Clearer answers when a search cannot give results.** `locus_search` now tells these apart:
  - **no access**: the API key was not accepted, or the plan or trial is not active;
  - **a used-up limit**, with the time it resets (or, for a limit with no reset time, what to do
    instead);
  - **an embedding service that did not answer**;
  - **an empty index** ("nothing indexed yet");
  - **a folder with nothing indexed in it** (with `path_prefix`).
  Each says that nothing was searched, so an assistant cannot report it as "no matching files".
  A computer that is offline is still reported by the hosted relay, as before.
- **"Update Locus" when Locus's service needs a newer version.** When the hosted relay refuses
  this version (HTTP 426, code `client_outdated`), `locus index`, `locus watch`, `locus_search` and
  `locus serve --transport relay` say so and show the update commands: `uv tool upgrade locusfiles`
  (or `pipx upgrade locusfiles`), then restart. Indexing stops once instead of failing file by
  file, and the relay worker exits with code 2 instead of 1, so a supervisor does not restart it in
  a loop. `locus serve` still writes nothing to stdout.
- **Stale results are flagged.** When a result's file changed on disk or was deleted after it was
  indexed, `locus_search` ends with a "Stale index" line that names those files. Re-run
  `locus index <folder> --prune`, or keep `locus watch` running.
- **`locus_read_file`** says "no access" when the operating system refuses to let Locus read an
  indexed file (for example a macOS privacy setting), instead of saying the file was moved.
- **`locus_diagnose_connection`** adds an "index state" line: `not_indexed`, `stale` (how many
  indexed files changed or are gone), or `ready`, plus when the index last changed. It shows counts
  only, never paths. With `check_remote=true` it also says whether Locus accepts this computer's
  key and the plan is active.
- `locus index` now records the new modified time of a file that was touched but not changed
  (same content). It does not embed the file again.
- **Reinstall hints lead with uv.** When `locus serve --transport relay` is missing the `relay`
  extra, or the bundled help articles cannot be read, the message now shows the
  `uv tool install --reinstall --python 3.12` command first and pipx as the alternative, as the
  README does. The relay hint also names `"locusfiles[local,relay]"`, so a reinstall does not drop
  on-device embeddings. The same order now holds when an embedder's optional dependency is missing
  (`LOCUS_EMBEDDER=local`, `voyage` or `openai`) and on `locus doctor`'s dependency FAIL lines;
  the `pip install -e` command for a source checkout comes last.
- **Help articles (`locus_help`) cover the new commands and answers.** The install, embedder, API
  key, trial and connect articles explain `locus setup --managed` and `--local`. The troubleshooting
  articles explain each answer `locus_search` now gives instead of results (empty index, a folder
  with nothing indexed, no access to the index folder, no access by key or plan, stale index), the
  index states `locus_diagnose_connection` reports, and what to do when the on-device model cannot
  be downloaded.

### Fixed

- **`locus serve --transport relay` starts without a hand-set key (connector gap G1).** Versions
  up to 0.1.0 refused to start unless `LOCUS_MCP_RELAY_SUPABASE_ANON_KEY` was set, and no public page
  named it, so Claude.ai and ChatGPT on the web failed for everyone who followed the Connect page.
  The value is the Locus project's public key (not a secret: it is the same value the Locus website
  sends to every browser). Now the relay's token answer (`/api/relay/token`) carries it as
  `realtime_public_key`, and Locus uses it when the variable is not set. The variable, when set, is
  still used first, so a staging relay or a pinned value works as before. Locus takes only that key
  from the answer, never an address, checks that it is shaped like a public key (and refuses a
  privileged one), and never prints it. If neither the variable nor the answer gives a usable key,
  the relay still stops with exit code 2 and the message now names the page that shows the value
  (`https://locusfiles.com/docs/connect#relay-key`). The token endpoint is now asked before that
  check, so a relay outage at start shows its own error (exit code 1, safe to restart) instead of
  a missing-variable error. Needs a Locus website that publishes the field; against a site that does
  not yet, set the variable as before.
- **`locus serve --transport relay` survives a network loss of any length.** Before, a computer
  that was offline or asleep for longer than a relay token lasts (about ten minutes) stopped with
  "could not refresh the relay token before it expired … Restart", and stayed off until someone
  restarted it. Now the relay waits it out. It prints one line, `relay offline: <reason>; retrying
  every ≤60 s`, and tries again after 1 s, 2 s, 4 s and so on, then every 60 s (each wait a little
  shorter at random, so computers do not all retry at once), for as long as it takes. Once the
  network is back it gets a fresh token, joins its channel again and prints `relay back online
  after Nm`. When the realtime connection gives up reconnecting by itself ("Connection failed
  permanently"), Locus opens a new connection. While offline, only the first few retries print a
  line, then at most one line a minute, and the connection library's own retry lines are muted.
  This covers an unreachable server, DNS failures, timeouts and server errors (5xx), including a
  relay that Locus has paused (503 `paused`). A rejected key (401, after two retries), an inactive
  plan (402) or an engine that is too old (426) still stops the relay with exit code 2. Exit code
  1 is now left for a relay that cannot be reached at startup, or one that keeps refusing the
  channel although it carries fresh tokens. `locus serve` still writes nothing to stdout.
- **No traceback when the on-device model cannot be downloaded.** With `LOCUS_EMBEDDER=local`, the
  first run downloads the model from huggingface.co. When that fails (offline, a proxy or firewall
  that blocks huggingface.co, or `HF_HUB_OFFLINE` set before any copy is on the computer),
  `locus index`, `watch` and `query` now say what failed and what to do, and exit with code 2:
  check the connection or the proxy (`HTTPS_PROXY`), set `HF_HUB_OFFLINE=1` when a copy is already
  downloaded, or use a Locus plan with `locus setup --managed`. Before, this was a long traceback,
  or "no such folder" with `HF_HUB_OFFLINE` set. `locus serve` answers with the same text, writes
  nothing to stdout, and tries the download again on the next request, so no restart is needed
  once the computer is back online. `locus doctor` shows the same text on its FAIL line.

## [0.1.2] - not published

Never published; these changes first ship in 0.1.3.

### Added

- **More file types, read with the Python standard library only (no new dependencies):**
  - PowerPoint `.pptx`: the text of each slide in slide order under a `Slide N` line, and the
    speaker notes under `Notes:`.
  - Excel `.xlsx`: each sheet as `Sheet: <name>` followed by its non-empty rows, cells separated
    by tabs. A sheet is cut at 5,000 rows and 200 columns, a whole workbook at 200,000
    characters by default, and the text says so; set `LOCUS_SPREADSHEET_MAX_CHARS` to a higher number
    (or `0` for no limit) to index more. Dates come through as the
    number Excel stores.
  - OpenDocument `.odt`, `.ods` and `.odp` (LibreOffice and OpenOffice): the same text, sheet and
    slide rules.
  - `.epub` books in reading order, and `.rtf` documents (including Hebrew and other legacy code
    pages).
  - Jupyter `.ipynb` notebooks: the Markdown and code cells, not their outputs.
  - `.markdown`, `.mdx`, `.org`, `.tex` and `.tsv` text files.
  - `.log`, `.ini` and `.cfg` can be read too but are not indexed by default (logs are large and
    noisy, config files often hold credentials); list them in `LOCUS_EXTENSIONS`, together with the
    other extensions you want, to include them.
- **Safety for these files:** a zip member that unpacks to more than 50 MB (or 200 MB in total) is
  refused, as is any XML part that declares a DOCTYPE or entities. Password-protected files, DRM
  EPUBs and corrupt files are reported as errors by `locus index`, which goes on with the rest of
  the folder, like a damaged PDF. Their earlier chunks, if any, stay in the index.
- **A new help article for `locus_help`** (21 bundled now): "The assistant didn't use Locus, or the
  first remote question timed out". It says what to try today in a web AI app with many connectors
  (say "use Locus", name the connector, add a line to the project or custom instructions, switch off
  connectors you do not need, ask again after a cold start), without promising that the app will
  pick Locus.

### Changed

- **Assistants reach for Locus on their own:** the descriptions of `locus_search`,
  `locus_list_sources` and `locus_read_file`, and the server instructions, now say plainly that
  Locus covers the user's own documents, notes and files on their computer, and that an assistant
  should search it first whenever a question could be answered from that material (projects, plans,
  meetings, notes, drafts, reports, code), even when you do not say "Locus" or "my files". They also
  say what it is not for: general knowledge, current events and questions about the outside world.
  Before this, a Claude.ai chat asked "Which two documents disagree about the launch date?" searched
  Google Drive and past chats and never called Locus until it was told to. The descriptions stay
  static text: no folder or file names are added to them.

- **A faster first answer after `locus serve` starts:** the server now loads the search model and
  opens your index in a background thread the moment it starts (stdio, `--transport relay` and
  `--transport streamable-http`), instead of on your first question. A cold local model could take
  longer than Claude.ai or ChatGPT wait for the relay, so the first remote question failed with
  "did not answer within 25 seconds" and the retry worked. The connection is never delayed by it, a
  failure is only logged to stderr (the first question then does the work as before), a hosted
  embedder is not called, and nothing is created when you have not indexed anything yet. Turn it off
  with `LOCUS_WARMUP=0`.

### Unchanged, for the record

- Legacy binary Office files (`.doc`, `.xls`, `.ppt`), Apple iWork files and images are still not
  read, and a scanned PDF still has no text to index.
- A big workbook can be a lot of text to embed. On a hosted embedder or a Locus plan, run
  `locus estimate <folder>` before the first index of a folder with large spreadsheets.

## [0.1.1] - not published

Never published; these changes first ship in 0.1.3.

### Added

- **`locus_help`, a sixth read-only tool:** ask your assistant how to do something with Locus or
  what an error means, and it answers from help articles that ship inside the engine. It covers
  installing and updating, indexing, connecting each AI app, local and managed embeddings, plans,
  limits and the trial, API keys, what leaves your computer, cancelling, deleting an account,
  teams, common errors and how to reach a person. Each answer names the page on the Locus website
  it comes from. It works offline and without an account, and it never mixes in your own files.
- **Answers in your language:** `locus_help` and `locus_account_status` now end with a short note
  asking the assistant to relay the answer in the language of your conversation, keeping Locus's
  product terms in English in brackets. The help articles themselves are in English.

### Changed

- **Default address of Locus's hosted services:** `https://locusfiles.com` (was the temporary
  `https://locus-rwhy.vercel.app`). It is used only by the `managed` embedder, the relay transport
  and the support tools. Version 0.1.0 installs keep working against the old address. Set
  `LOCUS_API_BASE` to override it, as before.
- **Clearer tool descriptions:** each tool now says when to use it and which tool to use instead,
  so assistants pick `locus_search` for your files and the support tools for questions about Locus.
  `locus_account_status` points to `locus_help` for how plans work, and `locus_diagnose_connection`
  reports how many help articles are bundled.
- **Wording about managed embeddings:** the notice `locus index` shows before it sends anything
  now says that, on managed plans, your search queries pass through Locus's relay to Voyage AI
  too, not only chunk text. This is what already happened; the notice now says it.

### Fixed

- `locus serve --transport relay` without the `relay` extra installed now exits with code 2 and
  prints the install command, instead of a traceback and exit code 1.

### Unchanged, for the record

- The relay transport still runs only remote requests the hosted relay has signed, and still
  exits at start while the relay publishes no signing key.

## [0.1.0] - 2026-09-29

First public release.

### Added

- **Install from PyPI** as `locusfiles` with pipx or `uv tool`. Extras:
  - `local`: on-device embeddings (sentence-transformers; downloads PyTorch);
  - `relay`: a Locus plan (managed embeddings, remote connection), no PyTorch;
  - `voyage`, `openai`: embed with your own provider key.
  Python 3.11 or newer. macOS and Linux are the tested platforms; Windows is not yet verified.
- **Indexing:**
  - `locus index <folder> [--prune]` parses, chunks, embeds and indexes a folder into a local
    Chroma store under `~/.locus` (`LOCUS_DATA_DIR`). Only chunk text, vectors and metadata are
    stored there, never copies of your files.
  - Re-runs skip unchanged files (content hash); `--prune` removes files that no longer exist.
  - `--dry-run`, and `locus estimate <folder>`, show what would be indexed and sent before anything
    is embedded.
  - With a hosted embedder, `locus index` shows what will be sent and where, and asks first
    (`--yes` to skip the prompt).
- **Search and serving:**
  - `locus query "<question>" -k 5` searches from the terminal.
  - `locus serve` runs the MCP server over stdio for local MCP clients (Claude Desktop, Claude Code,
    Cursor and others that can start a local server), with the tools `locus_search`,
    `locus_list_sources` and `locus_read_file`.
  - Every search result carries the file `path` and `chunk_index`, so the assistant can cite its
    source.
  - `locus_read_file` reads only files that are already in the index.
  - Two support tools: `locus_account_status` and `locus_diagnose_connection`.
- **Keeping the index current:** `locus watch <folder>` indexes a folder and then re-indexes it
  when files change.
- **Diagnostics:** `locus doctor` checks Python, packages, settings, the data directory and an
  embedder round trip. `locus stats` shows what is indexed.
- **Embedders:**
  - `local`: bge-small, English-only; the default.
  - `managed`: a Locus plan with `LOCUS_API_KEY`.
  - `voyage` and `openai`: your own key.
  One embedding model per index; switching models means re-indexing into a new collection.
- **Notion:** `locus sync-notion` pulls and indexes Notion pages. It needs your own Notion
  integration: set `NOTION_ACCESS_TOKEN`, or `NOTION_CLIENT_ID` and `NOTION_CLIENT_SECRET` for a
  one-time OAuth sign-in.
- **Remote transports for web assistants:**
  - `locus serve --transport relay`: outbound-only, through Locus's hosted relay. Needs a Locus
    plan, `LOCUS_API_KEY`, `LOCUS_MCP_RELAY_SUPABASE_ANON_KEY` (a public value) and the `relay`
    extra. The engine runs only remote requests the relay has signed.
  - `--transport streamable-http`: for a tunnel you run yourself; needs `LOCUS_MCP_TOKEN`.
  - Web assistants are not yet verified end to end with this release.
- **Licence:** FSL-1.1-ALv2. Each version converts to Apache-2.0 two years after it is made
  available. The wheel and sdist ship the licence, a NOTICE and the third-party licence inventory.

### Deprecated

- `locus_list_sources`' `prefix` argument is an alias for `path_prefix`. It is kept for the 0.1.x
  releases and removed in 0.2.

### Known limitations

- Windows is not yet verified.
- The relay transport exits at start ("the Locus relay published no request-signing key") until
  the hosted relay signs requests. That is switched on server side; no new release is needed.
- The engine reaches Locus's hosted services, which are used only by the `managed` embedder, the
  relay transport and the two support tools, at a temporary address
  (`https://locus-rwhy.vercel.app`). A later release moves the default to the permanent domain.
  The temporary address is not redirected for these requests when the domain moves, and
  `LOCUS_API_BASE` overrides the address.
