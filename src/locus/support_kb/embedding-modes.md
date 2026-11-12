---
id: embedding-modes
title: Local, managed or your own key: choosing an embedder
url: /free
keywords: embedder, embedding, embeddings, embedding model, model, local, managed, voyage, openai, LOCUS_EMBEDDER, locus setup, api key, switch embedder, change model, collection, LOCUS_COLLECTION, language, languages, multilingual, english, hebrew, quality, offline, hugging face
updated: 2026-10-02
---
`LOCUS_EMBEDDER` decides where the text of your files is turned into vectors for search:

- `local` (the default): an on-device model (bge-small, English-only). No account, no API key, no
  cost. Needs the `local` extra. The first time it loads, the model is downloaded from Hugging Face.
- `managed`: a Locus plan or trial. Set `LOCUS_API_KEY` from your dashboard. Needs the `relay` extra
  (no PyTorch).
- `voyage` or `openai`: your own provider account. Set `VOYAGE_API_KEY` or `OPENAI_API_KEY`, and
  install the matching extra.

`locus setup --local` and `locus setup --managed` set `LOCUS_EMBEDDER` for you (`--managed` also
checks and saves your key) in the `.env` file Locus reads in the folder you run them from.
For `voyage` or `openai`, set the variables yourself.

The free local mode is optimized for English; managed mode uses a multilingual model. We have not
yet measured search quality in other languages.

**One embedding model per index.** Vectors from two models cannot be compared, so Locus refuses to
mix them: after switching embedder you get an error saying the collection "was built with
embedder ...". To switch, set a new `LOCUS_COLLECTION` name (or delete `~/.locus/chroma`) and run
`locus index` on your folders again, then restart your AI app. `locus setup` does the first step:
for example `locus setup --managed --collection NAME` saves the new embedder and collection name
together, and it changes nothing if you leave out `--collection` while the old index is there.

What leaves your computer differs by mode: see "What leaves your computer, by mode".
