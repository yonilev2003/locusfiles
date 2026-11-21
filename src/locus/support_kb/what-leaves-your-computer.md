---
id: what-leaves-your-computer
title: What leaves your computer, by mode
url: /privacy
keywords: privacy, private, data, data flow, leave, leaves, upload, uploaded, send, sends, sent, share, store, stored, cloud, server, servers, security, secure, encrypted, voyage, vercel, supabase, hugging face, files, excerpts, who sees, safe, confidential
updated: 2026-11-17
---
What leaves your computer depends on the mode you use. This was checked against the engine's code
(version 0.2.0) on 2026-11-14. Statements about what Locus's hosted servers do or do not keep are
Locus's own and cannot be checked from the engine's code.

- **Local:** Indexing and embeddings run on your computer. The only request is the first-run
  download of the embedding model from Hugging Face. When your AI assistant searches, the matching
  excerpts go to that assistant, as they would if you pasted them in.
- **Your own key:** Chunk text goes straight from your computer to the embedding provider you
  chose, under your own account, and so does each search query.
- **Managed (off by default; needs `LOCUS_ENABLE_HOSTED=1` and a Locus account key):** Chunk text and your search queries pass through Locus's relay to
  Voyage AI to be embedded, with your account key and the Locus version. Locus says it does not
  store them and keeps only usage counts.
- **Remote (off by default; needs `LOCUS_ENABLE_HOSTED=1` and a Locus account key):** When you reach your computer from another device or a web AI app, your question and
  the matching excerpts travel through Locus's relay (Vercel and Supabase) to and from your
  computer. Locus says they are encrypted in transit and not stored. To connect, the engine also
  sends your account key, the Locus version and a random 8-character computer tag (plus the label
  in `LOCUS_DEVICE_LABEL`, if you set one).
- **Notion (off by default; needs `LOCUS_ENABLE_NOTION=1`):** `locus sync-notion` signs in to
  Notion in your browser, reads the pages your integration can see from Notion, and copies their
  text as plaintext Markdown files into `~/.locus/sources/notion/`, readable by anything that can
  read that folder. A page deleted in Notion stays there until you delete it. The files are then
  indexed like any folder, so your chosen embedder receives their text.
- **Account status:** `locus_help` with `check=true` adds the account status only when `LOCUS_API_KEY` is set. It,
  `locus setup --managed` (off by default) and `locus_help` with `check_remote` on
  send your account key to Locus's account-status endpoint (a read-only check), only when a key
  is set.
- **Account (only with the earlier hosted relay, which is off by default):** Locus says account details are
  stored with Supabase and your files are not; that is behaviour of its hosted servers, which the engine's
  code cannot show.
- **In every mode:** Locus does not upload your raw files. That is not the same as saying no data
  leaves your computer: the next paragraph says what your assistant receives.

Two details people often ask about:

- In every mode, search results (excerpts, file text the assistant reads, and file paths and
  names) go to the AI app you chose and on to its model provider. That provider's own policies
  apply to them.
- With the local embedder, the engine looks for a downloaded copy of its model first and contacts
  Hugging Face only to download it (the first time, or if the copy is gone). Hugging Face sees your
  IP address and the model name, not your files.

The privacy page has the full table, including what Locus keeps for each mode.
