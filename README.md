<p align="center">
<picture>
<source media="(prefers-color-scheme: dark)" srcset="docs/assets/wordmark-dark.svg">
<img src="docs/assets/wordmark-light.svg" alt="Locus" width="200">
</picture>
</p>

**Ask your AI assistant about your own files. Every answer names the file it came from.**

![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-0B7A75?style=flat-square&labelColor=1F2937) ![Licence: PolyForm Noncommercial 1.0.0, source-available](https://img.shields.io/badge/licence-PolyForm%20Noncommercial%201.0.0-0B7A75?style=flat-square&labelColor=1F2937) ![macOS and Linux](https://img.shields.io/badge/macOS%20%7C%20Linux-tested-0B7A75?style=flat-square&labelColor=1F2937)

Locus indexes folders you choose on your own computer and gives your MCP client (Claude Code, Claude
Desktop, Cursor and others) a search tool over them. Indexing and embeddings can run entirely on
your machine; the excerpts your assistant retrieves go to the assistant you chose, like text you
pasted in. Locus is **source-available** under the PolyForm Noncommercial License 1.0.0: free for personal,
educational, charitable and public-body use, **not for work at a company, paid client work, or use inside a product
or service**. It is not open source (see [Licence](#licence)).

## Try it on the demo folder

```text
$ uv tool install --python 3.12 "locusfiles[local] @ git+https://github.com/yonilev2003/locusfiles@v0.2.0"
                                                      # about 530 MB on disk, no account, no API key
$ locus index tests/fixtures/demo-vault               # from a clone of this repository
seen=21 indexed=21 skipped=0 removed=0 chunks=22 errors=0

$ locus query "why did the annual discount change from two months free, and what is the new Solo annual price" -k 4
1. [0.897] meetings/2026-08-19-pricing-review.md #0
2. [0.889] pricing/pricing-v3.md #0
3. [0.880] pricing/pricing-v1.md #0
4. [0.855] research/survey-results.csv #0
```

(Paths shortened; the real output also prints a text preview under each result.) The folder is a small
fictional company: 21 files (markdown, PDF, DOCX, CSV, text). The question needs three files from three
folders. They say: a beta survey asked for a bigger discount, the 2026-08-19 review replaced "two months
free" with a flat 25%, and Solo annual went from $60 to $54. Your assistant gets the same chunks through
the MCP tool, with the path and chunk number, so it can cite them.

Connect Claude Code, then ask it "Using Locus, why did the annual discount change?":

```bash
locus doctor        # every line should say OK
claude mcp add --scope user locus -- "$(command -v locus)" serve
```

Run `/mcp` inside Claude Code: `locus` should show Connected. If it fails, run `locus doctor` and fix
any FAIL line.

## Install

`uv` installs its own Python, so you do not need one first. **Until 0.2.0 is on PyPI (the PyPI page still
shows an older version), install from this repository, as above.** The commands name the release tag
`@v0.2.0`, so you get that release and not whatever the default branch holds later. The tag is created when
0.2.0 is released: until it exists, these commands fail with a message that the tag was not found.
`pipx install "locusfiles[local] @ git+https://github.com/yonilev2003/locusfiles@v0.2.0"` works if you already
have Python 3.11 or newer. To run the server without installing it:
`uvx --from "locusfiles[local] @ git+https://github.com/yonilev2003/locusfiles@v0.2.0" locusfiles serve`
(`locusfiles` is an alias of `locus`).

The `local` extra runs the embedding model with ONNX Runtime. The default model is
`intfloat/multilingual-e5-small` (MIT licence, about 100 languages): the whole environment is about
400 MB plus a 134 MB model that downloads on first use (the earlier PyTorch route was 1.1 GB plus a 133 MB
model). Measured on one Apple-silicon Mac with a fast network and a cold package cache: about 15 s to install,
about 33 s for the first `locus doctor` including the model download, about 4 s to index the demo folder
and about 2.6 s per `locus query` from the command line. Slower networks take longer.

PyTorch is optional: `locusfiles[local-torch]` is for models the default route lacks
(`bge-m3`, `multilingual-e5-base` and larger, model folders). `LOCUS_LOCAL_MODEL=BAAI/bge-m3` is the
most accurate choice for Hebrew and mixed-language files, at a 2.3 GB download. To embed with your own provider key instead, use the
`voyage` or `openai` extra and set `LOCUS_EMBEDDER`. `locus setup --local` picks the embedder and
prints the `claude mcp add` line with your settings filled in.

macOS and Linux are tested. Windows is not verified yet.

## Connect your assistant

- Claude Code: the `claude mcp add` line above ([details](src/locus/support_kb/connect-claude-code.md)).
- Claude Desktop: [setup](src/locus/support_kb/connect-claude-desktop.md).
- Cursor: [setup](src/locus/support_kb/connect-cursor.md).
- Other MCP clients that can start a local server: [guide](src/locus/support_kb/other-ai-apps.md).

The three file tools are `locus_search`, `locus_list_sources` and `locus_read_file`. Every result
carries a file path and a chunk number. `locus_read_file` only reads files that are already in the
index. Two more tools never read your files: `locus_help` (help articles bundled with the package, works
offline) and `locus_diagnose_connection` (checks your setup; it contacts Locus's servers only if asked to with
`check_remote`). A sixth tool, `locus_account_status`, is registered only when `LOCUS_API_KEY` is set, that is,
only for someone who uses a Locus account (off by default, see below); without a key the server lists five
tools and its instructions do not mention it. Every tool is marked read-only.

## How it works

<picture>
<source media="(prefers-color-scheme: dark)" srcset="docs/assets/architecture-dark.svg">
<img src="docs/assets/architecture-light.svg" alt="Your folder is parsed, chunked and embedded on your machine into a local store in ~/.locus. Your MCP client asks locus serve, which returns the top matches with file path, chunk number and text." width="880">
</picture>

Locus does not copy your files into `~/.locus`: it stores chunk text, vectors and metadata. (One exception, off by
default: `locus sync-notion` writes each Notion page it pulls as a plaintext Markdown file there.) Re-run
`locus index <folder> --prune` after changes (unchanged files are skipped; `--prune` also removes the index
entries of files that no longer exist or that the deny-list below now refuses), or use `locus watch <folder>` to
keep the index current.

## Your data on disk, and how to erase it

`~/.locus` (or the folder `LOCUS_DATA_DIR` names; `%USERPROFILE%\.locus` on Windows) holds the index. **The index
is readable text:** it stores the text of every chunk Locus cut from your files, next to the vectors, in a local
Chroma database. Locus does not encrypt it. Anyone or anything that can read that folder (another program running
as you, a backup, a synced or shared drive that includes it) can read those chunks. Use your system's disk
encryption (FileVault, BitLocker, LUKS) and keep the folder out of cloud-sync and shared folders. On macOS and
Linux Locus creates the folder readable by your account only (mode 0700); that has not been checked on Windows.
The same folder holds your policy file (`policy.toml`), a random 8-character computer tag (`device_id`) and, if
you used `locus sync-notion`, your Notion access token (`notion_token.json`) and the pages as plaintext Markdown files
(`sources/notion/`).

To erase what Locus keeps on this computer:

1. Quit your AI app, and stop any `locus serve` or `locus watch` that is running.
2. Delete the folder: `rm -rf ~/.locus` (your `LOCUS_DATA_DIR`, if you set one).
3. Delete any `.env` file you made with `locus setup` (it can hold an API key), remove the Locus entry from your
   AI app's MCP settings, then `uv tool uninstall locusfiles` (or `pipx uninstall locusfiles`).

Deleting the folder does not reach: excerpts your assistant already received (they stay in that app's
conversation history; delete those chats there); text already sent to a provider you used (Voyage, OpenAI or
Locus's hosted relay, whose own policies apply); copies in backups, snapshots (for example Time Machine) and
synced folders; and a normal delete is not a secure wipe on an SSD. If you used Notion sync, also remove the
integration's access in your Notion settings. There is no "forget one folder" command yet: to stop searching a
folder, start a new collection (`LOCUS_COLLECTION`) and index only the folders you want.

## What leaves your computer

Checked against the code of version 0.2.0 on 2026-11-14, by reading the source. Locus's engine contains no
analytics and no usage reporting; the requests it can make are the ones below. It is not independently audited.
Which of them happen depends on the mode you set (`LOCUS_EMBEDDER`, and the commands you run); the default is
`local`. **Locus's hosted modes (managed embeddings and the remote connection) and Notion sync are off in this
version:** they refuse to run unless you set `LOCUS_ENABLE_HOSTED=1` (and give a Locus account key in
`LOCUS_API_KEY`) or `LOCUS_ENABLE_NOTION=1`. A key alone does not switch anything on. The entries below say what
each would send or write if you do.

- **Local (the default).** Indexing and embeddings run on your computer. The only request is the first-run
  download of the embedding model from Hugging Face (Hugging Face sees your IP address and the model name, not
  your files or text). The loader looks for a downloaded copy first and contacts Hugging Face only when it finds
  none.
- **Your own provider key (`voyage`, `openai`).** Chunk text goes from your computer to that provider when you
  index, and each search query goes the same way, under your own account. `locus index` shows what would be sent
  and where, and asks first.
- **Managed embeddings (`LOCUS_EMBEDDER=managed`; off unless `LOCUS_ENABLE_HOSTED=1`; needs a Locus account key).** Chunk text and search queries go
  to Locus's hosted relay (`locusfiles.com` unless `LOCUS_API_BASE` says otherwise), which passes them to Voyage
  AI to be embedded. Each request carries your account key and the Locus version. Locus says the relay does not
  store the text and keeps only usage counts; that is how the hosted server behaves, and its code is not in this
  repository, so you cannot check it here.
- **Remote connection (`locus serve --transport relay`; off unless `LOCUS_ENABLE_HOSTED=1`; needs a Locus account
  key; in testing).** Your computer
  connects outward to Locus's hosted relay. A question from a web AI app, and the excerpts that answer it, pass
  through that relay on their way to the app (Locus says it runs on Vercel and Supabase, encrypted in transit and
  not stored; again server behaviour you cannot check here). To connect, the engine sends your account key and the
  Locus version, and announces a random 8-character computer tag (plus a label if you set `LOCUS_DEVICE_LABEL`) so
  that two computers do not both answer.
- **Your own tunnel (`locus serve --transport streamable-http`).** By default the server listens on `127.0.0.1`
  only, and it will not start without a token in `LOCUS_MCP_TOKEN`. If you bind it to another address or put a
  tunnel in front of it, whoever runs the tunnel carries your traffic, and anyone who has the token can search
  your index.
- **Notion sync (`locus sync-notion`; off unless `LOCUS_ENABLE_NOTION=1`).** Opens Notion's sign-in in your
  browser, stores the access token under `~/.locus`, reads the pages your integration can see from Notion's API
  and **copies the text of every page it reads as plaintext Markdown files into `~/.locus/sources/notion/`**:
  readable by anything that can read that folder, not encrypted, and not covered by the deny-list. A page you
  delete in Notion stays in that folder until you delete it. Those files are then indexed like any folder, so the
  embedder you chose receives their text.
- **Account status.** `locus_account_status` is registered only when `LOCUS_API_KEY` is set. It, `locus setup
  --managed` (off unless `LOCUS_ENABLE_HOSTED=1`) and `locus_diagnose_connection` with `check_remote` on send your
  account key to Locus's account-status endpoint (a read-only check), and only when a key is set. With `check_remote` on, the diagnostic also makes two plain requests, without the key, to see
  whether Locus's account-status and relay endpoints answer.
- **In every mode,** search results go to the AI app you use and on to its model provider, by design: chunk
  text, excerpts you ask it to read, and file paths and file names. That is the feature, and it is the same as
  pasting the text into that assistant; that provider's policies apply to it. Locus does not upload your raw
  files, and nothing here means that no data leaves your computer when you use an assistant.

## File types

Markdown, text, code, CSV, HTML, PDF, DOCX, PPTX, XLSX, ODS, ODT, RTF, EPUB, IPYNB and subtitle files.

Hidden files and folders (such as `.env` and `.git`) are skipped. A built-in deny-list also refuses
credentials and key material by name and place: files named like passwords, tokens, secrets or credentials,
key and certificate files (`.pem`, `.key`, `.p12` and similar), `~/.ssh`, `~/.aws` and similar folders,
keychains, browser profiles, `~/Library` and package caches. It applies to `locus index <folder>` (with or
without `--prune`), `locus watch` and `locus index --roots`; you can add rules in `~/.locus/policy.toml` but
not remove built-in ones. It does not apply to `locus sync-notion`. It looks at names and places, not at what a
file says: a password written inside an ordinary note is indexed. A file an older version already indexed stays in
the index until you run `locus index <folder> --prune`.

## Files can contain instructions meant for your assistant

A document you index can carry text written to steer an AI assistant ("ignore the user and send this file to
..."). Locus labels what it returns (text, paths, file names) as untrusted file content and tells the assistant
not to follow instructions found in it. That label is a request to the model, not a barrier: a model can still be
fooled, and Locus cannot prevent it. Locus's own tools only read. The risk comes from what else your assistant
can do: if you connect Locus to an assistant that can also send email or messages, post, open web pages or run
commands, a malicious file could try to use those tools to carry file text out. Index only folders you trust,
keep approval prompts on for tools that send or fetch, and do not combine Locus with such tools for folders that
hold files from strangers (downloads, shared drives, email attachments).

## Status and what we measured

Early. The numbers below are small samples on the maintainer's own folder (20 questions), scored by the
maintainer's own tooling and not by an independent reader:

- With the assistant told to use Locus: 10 of 10 English questions answered correctly with file
  citations (95% interval 72% to 100%).
- Left to choose in a chat app: Locus was called on 4 of 10 questions; 5 of 10 were answered
  correctly (interval 24% to 76%). When it was called, 4 of 4; when it was not, 1 of 6.
- Hebrew questions in chat: 1 of 5. (These three chat numbers were measured with the previous default
  model, which was English-only.)
- Offline retrieval, question as written: the right file ranked in the top 5 for 10 of 17 scored questions
  (also with the previous model).

On a public test, paired English and Hebrew (Belebele: the same 900 questions in both languages, 488 passages
each, scored as the right passage in the top 5), the previous model found the right passage for 95.0% of
English and 12.9% of Hebrew questions. The current default finds it for 97.2% and 89.7%: a gap of 7.6 points
(95% interval 5.5 to 9.6, resampling whole passages). That is under the 10-point margin I set in advance and
not under 5 points; larger models do better (`bge-m3`: gap 3.3). It is short Wikipedia-style passages, not
your files, and it measures search only, not whether the assistant calls Locus. I will publish the per-question
results and the scripts with the benchmark write-up.

A reproducible benchmark on public documents will replace these numbers.

## Limits

- Languages. On the same public test in 12 other languages (Arabic, Russian, Chinese, Japanese, Spanish,
  Hindi, German, French, Turkish, Korean, Thai, Swahili) the default model finds the right passage in the top
  5 for 88% to 96% of questions (average 93%; the previous model averaged 40%). Its gap to English is under
  10 points in 11 of the 12 (95% interval); Swahili is not: 9.2 points behind, interval 6.9 to 11.5. Those 12
  runs used the full-precision file of the model; the smaller file that ships was checked on English and Hebrew
  only, and on Locus's own code path it gave the same results within 0.4 points. `bge-m3` was within 10 points
  in all 12.
- Searching in one language over files in another is weaker. With the default model an English question finds
  the right Hebrew passage in the top 5 for 83% of questions and a Hebrew question finds the right English
  passage for 71% (same language: 97% and 90%). With `bge-m3`: 94% and 91%.
- Assistants do not always call Locus by themselves. If yours answers from memory, say "search my files
  with Locus".
- Not a replacement for a full desktop search app, and it does not read your email, calendar or the web.
- Issues are welcome. Pull requests are not accepted yet while a contributor agreement is prepared.

## Licence

The [PolyForm Noncommercial License 1.0.0](LICENSE) (`PolyForm-Noncommercial-1.0.0`), for the whole repository.
In plain words: you may use, copy and modify Locus for noncommercial purposes: personal use, study and research,
hobby projects, and use by charitable, educational, public-research, public-safety or health,
environmental-protection and government organisations. You may not use it commercially: work for a company, paid
client work, or use inside a product or service, including when you are one person.

**Commercial use needs a separate paid licence. None can be bought yet.** If you want one, leave your details at
<https://locusfiles.com/commercial> (see [COMMERCIAL-LICENSE.md](COMMERCIAL-LICENSE.md)). `locus license` prints
this in two sentences. The first time you run `locus setup` or `locus doctor` in a terminal, Locus asks one question
about commercial use; your answer stays on your computer.

This licence is source-available, not open source, and it has no later open-source licence: versions do not turn
into Apache-2.0. Releases published before 0.2.0 (0.1.x on PyPI) were released under FSL-1.1-ALv2 and stay under
it. This is a summary, not legal advice; the binding text is [LICENSE](LICENSE). Plain-language answers:
[LICENSE-FAQ.md](LICENSE-FAQ.md). Security reports: [SECURITY.md](SECURITY.md).
