---
id: install
title: Install, update or uninstall the Locus engine
url: /docs/connect
keywords: install, installation, setup, set up, locus setup, configure, first run, download, pipx, uv, python, pypi, locusfiles, extra, extras, macos, mac, linux, windows, upgrade, update, uninstall, reinstall, version, managed, local
updated: 2026-11-17
---
Locus runs on your own computer. You need a terminal. The package is `locusfiles`; the command it
installs is `locus`. These steps were tested on one Mac (macOS); on Linux only inside an arm64 Debian container
(2026-11-17; running the test suite there needed `procps` and a raised open-file limit, and 8 tests were skipped),
not on a real Linux machine. Windows is not verified.

We recommend `uv`: you do not need Python installed first, because it installs its own. Get it with
`curl -LsSf https://astral.sh/uv/install.sh | sh`, then open a new terminal so `uv` is found
(skip both if you already have uv).
`pipx` works too if you already have Python 3.11 or newer.

Pick the extra that matches how your files will be embedded:

- On-device embeddings, no account (ONNX Runtime, about 530 MB installed with the model; PyTorch is optional):
  `uv tool install --python 3.12 "locusfiles[local]"` or `pipx install "locusfiles[local]"`
- Your own Voyage or OpenAI key: the `voyage` or `openai` extra.

Locus offers no hosted service in this version. The earlier relay (managed embeddings and the remote
connection, which the `relay` extra installs) is off by default (`LOCUS_ENABLE_HOSTED=1`) and still in
testing; you do not need it.

After a `uv` install, run `uv tool update-shell` once and open a new terminal, so the `locus`
command is found.

Next, set up on-device embeddings. Run this in the folder you will run `locus` from:

- `locus setup --local`: on-device embeddings, no account and no key. The first run downloads the
  model from huggingface.co once; later runs use the copy on your computer.

It writes its settings into the `.env` file Locus reads in that folder (a new file is
readable only by you; an existing one keeps its other lines) and prints the next steps, including
the settings to give your AI app. If your index was built with another embedder, it changes nothing
and asks for a new collection name: add `--collection NAME`.

Then run `locus doctor`. It checks Python, every dependency, your settings and the embedder, and
each FAIL line names what to fix. Re-run it until every line says OK.

Settings are environment variables, or a `.env` file in the folder you run `locus` from (or one of
its parents). Indexes live in `~/.locus` (set `LOCUS_DATA_DIR` to move them).

- Update: `uv tool upgrade locusfiles` (or `pipx upgrade locusfiles`), then restart your AI app.
- Repair a broken install: the same `uv tool install` command with `--reinstall`, or
  `pipx reinstall locusfiles`.
- Uninstall: `uv tool uninstall locusfiles` (or `pipx uninstall locusfiles`). Your local index stays
  in `~/.locus` until you delete that folder.
