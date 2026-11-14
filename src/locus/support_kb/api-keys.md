---
id: api-keys
title: API keys: create, use, revoke
url: /support
keywords: api key, api keys, key, keys, LOCUS_API_KEY, create key, generate key, new key, revoke, revoked, lost key, leaked, stolen, compromised, rotate, dashboard, invalid key, rejected, not recognized, new computer, key limit, locus setup, save key
updated: 2026-10-02
---
A Locus API key connects one computer to your account, for managed embeddings and the remote
connection. Local mode needs no key.

- **Create:** sign in, open your dashboard and choose "Create key". Copy it right away: it is
  shown only once. Put it on the computer it is for: run `locus setup --managed` there, which asks
  for the key without showing it, checks it with Locus and saves it as `LOCUS_API_KEY` in `.env`
  (or put `LOCUS_API_KEY` in `.env` yourself). Put it in your AI app's `env` block for the Locus
  server too, then restart `locus serve` or your AI app.
- **How many:** one key per computer you index. Your plan sets how many active keys you can have
  (the limit is set per account). Revoke a key you no longer use to free its place.
- **"Key not recognized" or "rejected":** the key was revoked or mistyped. Check the key list in your
  dashboard, generate a new key if needed, and update the computer: `locus setup --managed --new-key`
  replaces the saved key.

Anyone who holds your Locus API key can search and read your indexed files through the relay while
your computer is serving, take over as your relay computer, spend your managed-embedding budget, and
act as your account for up to ten minutes with each relay token they get from it. Keep it secret. If
it leaks, revoke it from the dashboard, then check your key list again after ten minutes and revoke
any key you did not create.
