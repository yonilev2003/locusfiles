---
id: free-trial
title: The free trial and the free local engine
url: /free
keywords: free, free trial, trial, no card, credit card, sign up, signup, trial end, trial ended, expire, expired, expiry, how long, tokens, token budget, free plan, free tier
updated: 2026-10-02
---
There are two different free things:

- **The local engine** (`LOCUS_EMBEDDER=local`): free, no account, no time limit. Indexing and
  embeddings run on your computer.
- **The hosted trial:** creating an account gives you managed embeddings without a Voyage key of
  your own. No credit card is needed. The trial has a 5,000,000-token budget for the whole trial.

How long the trial lasts: While paid plans are not open yet, a free trial does not run out by time.
Once they open, it runs until 7 days after they open or 14 days after you signed up, whichever is
later. Usage limits still apply: the trial's token budget, a daily limit, and at busy times a pause
on trial indexing until the next UTC day.

When the trial ends, by time or by its token budget, continuing with managed embeddings (and the
remote connection) means choosing a paid plan once they are open. The local engine keeps working
either way: switch with `locus setup --local --collection NAME` (a new collection name) and run
`locus index` on your folders again (see the embedder article).

To see your own trial end date and today's usage, ask your AI app to run `locus_account_status`, or
open your dashboard.
