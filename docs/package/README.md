# Locus

Your files, your machine, the MCP clients you already use.

Locus indexes folders you choose on your own computer and gives MCP clients that can start a local
server (setup pages exist for Claude Code, Claude Desktop and Cursor; on this version we have tested Claude
Code only) a semantic search tool over them. Every
result carries the file path, so the assistant can cite its sources.

The package is `locusfiles`; the command it installs is `locus` (and `locusfiles` as an alias of it,
so `uvx --from "locusfiles[local]" locusfiles serve` runs the server without installing it).

<!-- mcp-name: io.github.yonilev2003/locusfiles -->

## Install

We recommend [uv](https://docs.astral.sh/uv/): you do not need Python installed first, because it
installs its own. [pipx](https://pipx.pypa.io) works too if you already have Python 3.11 or newer.
Either one puts `locus` in its own environment. `locusfiles` has been on PyPI since 0.1.0
(2026-09-29):

```bash
# Recommended: uv (install it first; skip if `uv --version` already works)
curl -LsSf https://astral.sh/uv/install.sh | sh   # then open a new terminal, so `uv` is found
# On-device embeddings, no account (ONNX Runtime: about 400 MB installed plus a 134 MB multilingual model on first use):
uv tool install --python 3.12 "locusfiles[local]"
uv tool update-shell               # adds ~/.local/bin to PATH; open a new terminal afterwards

# Alternative: pipx (needs Python 3.11+)
pipx install "locusfiles[local]"
```

Other extras: `voyage` or `openai` to embed with your own provider key, and `local-torch` for
on-device models the default route lacks (`bge-m3`, `multilingual-e5`, model folders), which adds
PyTorch (the environment is about 1.1 GB).

Tested on one Mac (Apple silicon, macOS). On Linux (arm64, Debian 13), the test suite and a clean install passed inside
a container on 2026-11-17 (one run); that is not a real Linux machine, not x86_64, and no assistant was tried there.
Windows is not verified.

## First search

```bash
export LOCUS_EMBEDDER=local            # or voyage, openai (your own key)
locus doctor                           # checks Python, packages, settings and the embedder
locus index ~/Documents/notes --prune  # parse, chunk, embed and index a folder
locus query "what did I decide about pricing" -k 5
```

Then connect your AI assistant: it runs `locus serve` (MCP over stdio). Setup steps for each
client are on the Locus website's Connect page (`/docs/connect`).

## What leaves your computer

Checked against the code of version 0.2.0 on 2026-11-14. The repository README has the full statement.

- Local (the default): Indexing and embeddings run on your computer; the only request is the first-run
  download of the embedding model from Hugging Face. When your AI assistant searches, the matching excerpts go
  to that assistant, as they would if you pasted them in.
- ONNX Runtime (the library that runs the local embedding model): from version 1.31 its macOS and Linux builds
  include Microsoft's usage-telemetry uploader, on by default. Locus sets `ORT_DISABLE_TELEMETRY=1` when it is
  imported. Verified on macOS only (one Mac, ONNX Runtime 1.32.0); Linux and Windows are not verified. The
  repository README has the details.
- Your own key: Chunk text, and each search query, goes straight from your computer to the embedding provider
  you chose, under your own account.
- Managed (off by default: needs `LOCUS_ENABLE_HOSTED=1` and a Locus account key): Chunk text and your search queries pass through Locus's relay to Voyage
  AI to be embedded. Locus says it does not store them and keeps only usage counts; that is server behaviour
  the package's code cannot show.
- Remote (off by default: needs `LOCUS_ENABLE_HOSTED=1` and a Locus account key): When you reach your computer from a web AI app, your question and the
  matching excerpts travel through Locus's relay. Locus says they are encrypted in transit and not stored.
- Notion sync (off by default: needs `LOCUS_ENABLE_NOTION=1`): the text of the pages you pull is copied as
  plaintext Markdown files into `~/.locus/sources/notion/` and then indexed.
- Account status: the `locus_account_status` tool exists only when `LOCUS_API_KEY` is set; it sends that key to
  Locus's account-status endpoint when asked.
- In every mode: Locus does not upload your raw files. Search results, including file text and paths, go to
  the AI app you use and on to its model provider.

The index in `~/.locus` is readable, unencrypted text (the text of every indexed chunk). To erase it, delete
that folder.

Indexes live in `~/.locus` (set `LOCUS_DATA_DIR` to move them). Settings are environment
variables, or a `.env` file in the directory you run `locus` from or one of its parents.

## Licence

The `locusfiles` package is licensed under the **PolyForm Noncommercial License 1.0.0**
(`PolyForm-Noncommercial-1.0.0`). The full text ships inside the package as its `LICENSE` file, and that text
is the binding version. In short, the engine is **source-available**: you may use, copy, modify and redistribute
it for noncommercial purposes (personal use, study and research, hobby projects, and use by charitable,
educational, public-research, public-safety or health, environmental-protection and government organisations),
and not for work at a company, paid client work, or use inside a product or service. Commercial use needs a
separate paid licence, which cannot be bought yet: leave your details at https://locusfiles.com/commercial.
`locus license` prints this. Releases before 0.2.0 were released under FSL-1.1-ALv2 and stay under it. The hosted
services (the relay, the website and billing) are not part of the package and are not licensed by it. This is a
summary, not legal advice.
