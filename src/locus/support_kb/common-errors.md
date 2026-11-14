---
id: common-errors
title: Common error messages and their fixes
url: /support
keywords: error, errors, error message, fail, failed, FAIL, not configured, api key not set, VOYAGE_API_KEY, OPENAI_API_KEY, LOCUS_API_KEY, rejected, invalid, revoked, subscription isn't active, inactive, trial expired, budget, quota, daily limit, rate limit, 429, 402, 401, sentence-transformers, sentence_transformers, import error, module not found, was built with embedder, collection, LOCUS_API_BASE, doctor, could not load, model download, download failed, hugging face, huggingface, HF_HUB_OFFLINE, proxy, HTTPS_PROXY, ProxyError, 403 Forbidden, offline, no internet, traceback, locus setup, update locus, upgrade, outdated, too old, 426, LOCUS_MCP_RELAY_SUPABASE_ANON_KEY, public key, anon key
updated: 2026-10-03
---
Start with `locus doctor`: each FAIL line names the check that failed. The most common messages:

- **"VOYAGE_API_KEY is not set" / "OPENAI_API_KEY is not set" / "LOCUS_API_KEY is not set":** the
  embedder you chose needs a key that this process cannot see. Add it to `.env` (for a Locus plan,
  `locus setup --managed` does this), or to the AI app's `env` block for the Locus server, or switch
  to `LOCUS_EMBEDDER=local`.
- **"fastembed" or "sentence-transformers" not installed:** `LOCUS_EMBEDDER` is `local` (the default) but the engine
  was installed without the `local` extra (a model fastembed does not offer, such as bge-m3, needs `local-torch`). Either run `locus setup --managed` (with a Locus plan)
  or reinstall with the `local` extra: `uv tool install --python 3.12 --reinstall "locusfiles[local]"`
  (or `pipx install --force "locusfiles[local]"`).
- **"Locus could not load its on-device embedding model":** with `local`, the first run downloads
  the model from huggingface.co, and that failed: the computer is offline, a proxy or firewall
  blocks huggingface.co, or `HF_HUB_OFFLINE` is set and no copy was downloaded yet. Get online (behind
  a proxy, set `HTTPS_PROXY`, in the AI app's `env` block too), or unset `HF_HUB_OFFLINE` for one
  run, then try again. With a copy already downloaded, `HF_HUB_OFFLINE=1` loads it offline. Or use a
  Locus plan instead: `locus setup --managed`.
- **"Collection ... was built with embedder ... but the current embedder is ...":** you changed
  embedder. Use a new `LOCUS_COLLECTION` (or delete `~/.locus/chroma`) and re-index.
- **"LOCUS_API_KEY was rejected (invalid or revoked)":** generate a new key in your dashboard and
  replace it on this computer.
- **"Your Locus subscription isn't active":** the Locus account behind `LOCUS_API_KEY` is not active.
  `locus_account_status` (present when a key is set) shows which. Local search keeps working.
- **Daily limit or token budget reached (HTTP 429 or 402):** managed embedding pauses until the
  limit resets (daily limits reset at 00:00 UTC). `locus index` stops with a summary you can resume
  by running it again later. Search of what is already indexed keeps working.
- **"Update Locus: the Locus relay does not accept this version" (HTTP 426):** Locus's service needs
  a newer engine than this one. Run `uv tool upgrade locusfiles` (or `pipx upgrade locusfiles`),
  then restart Locus and the AI app that starts it.
- **"LOCUS_MCP_RELAY_SUPABASE_ANON_KEY is not set" (relay):** Locus 0.1.2 and older need this public,
  non-secret setting; the Connect page shows its value. Or update Locus
  (`uv tool upgrade locusfiles`, or `pipx upgrade locusfiles`): from 0.1.3 the relay supplies it.
  On 0.1.3 and it says "did not send its public key"? Set the variable from the Connect page.
- **"LOCUS_API_BASE ... is not a base URL":** remove or fix `LOCUS_API_BASE` in `.env`; normally it
  should not be set at all.
- **A dependency fails to import:** reinstall the way you installed (the same `uv tool install`
  command with `--reinstall`, or `pipx reinstall locusfiles`).
