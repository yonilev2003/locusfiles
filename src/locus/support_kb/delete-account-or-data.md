---
id: delete-account-or-data
title: Delete your account or your local data
url: /privacy
keywords: delete, deletion, delete account, remove, erase, close account, account, my data, personal data, export, copy of my data, correct, correction, right to be forgotten, uninstall, local index, wipe, clear index, remove folder, forget
updated: 2026-10-02
---
**Delete your account.** Signed in, open your dashboard and choose "Delete your account". This logs
a timestamped request that you can cancel while it is pending. It is not instant, automated
deletion: someone reviews and processes each request by hand. We delete your account and the data
kept with it, except records the law requires us to keep, such as invoices.

**See, correct or get a copy of your data.** Email us from the address on your account (the
address is on the privacy page). We answer within 30 days of receiving a request.

**Your local data.** Your local index is yours to manage. Delete it at any time by removing your
data directory (default `~/.locus`); Locus has no copy of it. Files you delete from disk leave the
index the next time you run `locus index <folder> --prune`. To stop searching a folder whose files
you keep, start a fresh collection (a new `LOCUS_COLLECTION` name) and index only the folders you
want. To remove the engine itself: `uv tool uninstall locusfiles` (or `pipx uninstall locusfiles`).

Deleting your account does not touch the files or index on your computer, and deleting your local
index does not close your account.
