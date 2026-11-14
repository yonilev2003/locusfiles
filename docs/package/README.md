# Locus

Your files, your machine, the MCP clients you already use.

Locus indexes folders you choose on your own computer and gives MCP clients that can start a local
server (works with Claude Desktop, Claude Code and Cursor; setup guides for other clients) a semantic
search tool over them. Every
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
# or a Locus plan (managed embeddings, remote connection), no on-device model:
uv tool install --python 3.12 "locusfiles[relay]"
uv tool update-shell               # adds ~/.local/bin to PATH; open a new terminal afterwards

# Alternative: pipx (needs Python 3.11+)
pipx install "locusfiles[local]"   # or "locusfiles[relay]"
```

Other extras: `voyage` or `openai` to embed with your own provider key, and `local-torch` for
on-device models the default route lacks (`bge-m3`, `multilingual-e5`, model folders), which adds
PyTorch (the environment is about 1.1 GB).

macOS and Linux are tested. Windows is not yet verified.

## First search

```bash
export LOCUS_EMBEDDER=local            # or managed (with LOCUS_API_KEY), voyage, openai
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
- Your own key: Chunk text, and each search query, goes straight from your computer to the embedding provider
  you chose, under your own account.
- Managed (needs a Locus account key): Chunk text and your search queries pass through Locus's relay to Voyage
  AI to be embedded. Locus says it does not store them and keeps only usage counts; that is server behaviour
  the package's code cannot show.
- Remote (needs a Locus account key): When you reach your computer from a web AI app, your question and the
  matching excerpts travel through Locus's relay. Locus says they are encrypted in transit and not stored.
- Notion sync: pages you pull are saved as Markdown files in `~/.locus/sources/notion/` and then indexed.
- Account status: your account key is sent to Locus's account-status endpoint when you ask for it.
- In every mode: Locus does not upload your raw files. Search results, including file text and paths, go to
  the AI app you use and on to its model provider.

The index in `~/.locus` is readable, unencrypted text (the text of every indexed chunk). To erase it, delete
that folder.

Indexes live in `~/.locus` (set `LOCUS_DATA_DIR` to move them). Settings are environment
variables, or a `.env` file in the directory you run `locus` from or one of its parents.

## Licence

The `locusfiles` package is licensed under the **Functional Source License, Version 1.1, ALv2
Future License** (`FSL-1.1-ALv2`). The full text ships inside the package as its `LICENSE` file,
and that text is the binding version. In short, the engine is **source-available**: you may use,
copy, modify and redistribute it for any purpose except a Competing Use (making it available to
others in a commercial product or service that substitutes for it or offers substantially similar
functionality). Two years after a version is made available, that version is also available under
the Apache License, Version 2.0. The hosted services (the relay, the website and billing) are not
part of the package and are not licensed by it.
