---
id: common-errors
title: Common error messages and their fixes
url: /support
keywords: error, errors, error message, fail, failed, FAIL, not configured, api key not set, VOYAGE_API_KEY, OPENAI_API_KEY, LOCUS_API_KEY, rejected, invalid, revoked, subscription isn't active, inactive, trial expired, budget, quota, daily limit, rate limit, 429, 402, 401, sentence-transformers, sentence_transformers, import error, module not found, was built with embedder, collection, LOCUS_API_BASE, doctor, could not load, model download, download failed, hugging face, huggingface, HF_HUB_OFFLINE, proxy, HTTPS_PROXY, ProxyError, 403 Forbidden, offline, no internet, traceback, locus setup, update locus, upgrade, outdated, too old, 426, LOCUS_MCP_RELAY_SUPABASE_ANON_KEY, public key, anon key
updated: 2026-11-17
---
Start with `locus doctor`: each FAIL line names the check that failed. The most common messages:

- **"VOYAGE_API_KEY is not set" / "OPENAI_API_KEY is not set":** the embedder you chose needs a key
  that this process cannot see. Either switch to `LOCUS_EMBEDDER=local` (the default: no key, no
  account), or put your key in `.env` or in the AI app's `env` block for the Locus server.
- **"fastembed" or "sentence-transformers" not installed:** `LOCUS_EMBEDDER` is `local` (the default) but the engine
  was installed without the `local` extra (a model fastembed does not list, such as bge-m3, needs `local-torch`).
  Reinstall with the `local` extra: `uv tool install --python 3.12 --reinstall "locusfiles[local]"`
  (or `pipx install --force "locusfiles[local]"`).
- **"Locus could not load its on-device embedding model":** with `local`, the first run downloads
  the model from huggingface.co, and that failed: the computer is offline, a proxy or firewall
  blocks huggingface.co, or `HF_HUB_OFFLINE` is set and no copy was downloaded yet. Get online (behind
  a proxy, set `HTTPS_PROXY`, in the AI app's `env` block too), or unset `HF_HUB_OFFLINE` for one
  run, then try again. With a copy already downloaded, `HF_HUB_OFFLINE=1` loads it offline.
- **"Collection ... was built with embedder ... but the current embedder is ...":** you changed
  embedder. Use a new `LOCUS_COLLECTION` (or delete `~/.locus/chroma`) and re-index.
- **A dependency fails to import:** reinstall the way you installed (the same `uv tool install`
  command with `--reinstall`, or `pipx reinstall locusfiles`).

**Only if you turned on Locus's hosted relay.** Locus offers no hosted service in this version; the
earlier relay (managed embeddings and the remote connection) is off by default (`LOCUS_ENABLE_HOSTED=1`)
and still in testing. If you did turn it on, switching back to `LOCUS_EMBEDDER=local` and unsetting
`LOCUS_API_KEY` is the simplest way out. The messages you may see:

- **"LOCUS_API_KEY is not set" / "LOCUS_API_KEY was rejected (invalid or revoked)":** the key the hosted
  relay needs is missing, or it was not accepted. Replace it on this computer, or leave the relay off.
- **"Your Locus subscription isn't active":** the hosted account behind `LOCUS_API_KEY` is not active.
  `locus_account_status` (present when a key is set) shows which. Local search keeps working.
- **Daily limit or token budget reached (HTTP 429 or 402):** managed embedding pauses until the
  limit resets. `locus index` stops with a summary you can resume by running it again later. Search of
  what is already indexed keeps working.
- **"Update Locus: the Locus relay does not accept this version" (HTTP 426):** the relay needs
  a newer engine than this one. Run `uv tool upgrade locusfiles` (or `pipx upgrade locusfiles`),
  then restart Locus and the AI app that starts it.
- **"LOCUS_MCP_RELAY_SUPABASE_ANON_KEY is not set" (relay):** Locus 0.1.2 and older need this public,
  non-secret setting. Or update Locus (`uv tool upgrade locusfiles`, or `pipx upgrade locusfiles`):
  from 0.1.3 the relay supplies it.
- **"LOCUS_API_BASE ... is not a base URL":** remove or fix `LOCUS_API_BASE` in `.env`; normally it
  should not be set at all.
