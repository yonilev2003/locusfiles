---
id: what-leaves-your-computer
title: What leaves your computer, by mode
url: /privacy
keywords: privacy, private, data, data flow, leave, leaves, upload, uploaded, send, sends, sent, share, store, stored, cloud, server, servers, security, secure, encrypted, voyage, vercel, supabase, hugging face, files, excerpts, who sees, safe, confidential
updated: 2026-10-01
---
What leaves your computer depends on the mode you use:

- **Local:** Indexing and embeddings run on your computer. When your AI assistant searches, the
  matching excerpts go to that assistant, as they would if you pasted them in.
- **Your own key:** Chunk text goes straight from your computer to the embedding provider you
  chose, under your own account.
- **Managed:** On managed plans, chunk text and your search queries pass through Locus's relay to
  Voyage AI to be embedded. We don't store them; we keep only usage counts.
- **Remote:** When you reach your computer from another device or a web AI app, your question and
  the matching excerpts travel through Locus's relay (Vercel and Supabase) to and from your
  computer. They are encrypted in transit and we don't store them.
- **Account:** Your account details are stored with Supabase. Your files are not.
- **In every mode:** Your raw files are never uploaded to Locus.

Two details people often ask about:

- In every mode, search results (excerpts, file text the assistant reads, and file paths) go to the
  AI app you chose and on to its model provider. That provider's own policies apply to them.
- With the local embedder, each time the engine loads its model while you are online it contacts
  Hugging Face (and downloads the model the first time). Hugging Face sees your IP address and the
  model name, not your files.

The privacy page has the full table, including what Locus keeps for each mode.
