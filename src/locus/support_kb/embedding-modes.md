---
id: embedding-modes
title: Local, managed or your own key: choosing an embedder
url: /free
keywords: embedder, embedding, embeddings, embedding model, model, local, managed, voyage, openai, LOCUS_EMBEDDER, locus setup, api key, switch embedder, change model, collection, LOCUS_COLLECTION, language, languages, multilingual, english, hebrew, quality, offline, hugging face
updated: 2026-11-14
---
`LOCUS_EMBEDDER` decides where the text of your files is turned into vectors for search:

- `local` (the default): an on-device multilingual model (multilingual-e5-small, about 100 languages). No account, no API key, no
  cost. Needs the `local` extra. The first time it loads, the model is downloaded from Hugging Face.
- `managed`: a Locus plan or trial. Set `LOCUS_API_KEY` from your dashboard. Needs the `relay` extra
  (no PyTorch).
- `voyage` or `openai`: your own provider account. Set `VOYAGE_API_KEY` or `OPENAI_API_KEY`, and
  install the matching extra.

`locus setup --local` and `locus setup --managed` set `LOCUS_EMBEDDER` for you (`--managed` also
checks and saves your key) in the `.env` file Locus reads in the folder you run them from.
For `voyage` or `openai`, set the variables yourself.

The default on-device model, multilingual-e5-small, covers about 100 languages. How well it finds
the right passage in other languages was measured on public test data (see "Limits" in the README),
not on your files.

**One embedding model per index.** Vectors from two models cannot be compared, so Locus refuses to
mix them: after switching embedder you get an error saying the collection "was built with
embedder ...". To switch, set a new `LOCUS_COLLECTION` name (or delete `~/.locus/chroma`) and run
`locus index` on your folders again, then restart your AI app. `locus setup` does the first step:
for example `locus setup --managed --collection NAME` saves the new embedder and collection name
together, and it changes nothing if you leave out `--collection` while the old index is there.

What leaves your computer differs by mode: see "What leaves your computer, by mode".
