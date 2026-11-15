---
id: delete-account-or-data
title: Delete your account or your local data
url: /privacy
keywords: delete, deletion, delete account, remove, erase, close account, account, my data, personal data, export, copy of my data, correct, correction, right to be forgotten, uninstall, local index, wipe, clear index, remove folder, forget
updated: 2026-11-14
---
**Delete your account.** Signed in, open your dashboard and choose "Delete your account". This logs
a timestamped request that you can cancel while it is pending. It is not instant, automated
deletion: someone reviews and processes each request by hand. We delete your account and the data
kept with it, except records the law requires us to keep, such as invoices.

**See, correct or get a copy of your data.** Email us from the address on your account (the
address is on the privacy page). We answer within 30 days of receiving a request.

**Your local data.** Your local index is yours to manage. The index holds the text of every chunk
Locus made from your files, as readable, unencrypted text next to the vectors, in your data
directory (default `~/.locus`, or the folder `LOCUS_DATA_DIR` names). To erase it: quit your AI app
and stop any `locus serve` or `locus watch`, delete that folder (`rm -rf ~/.locus`), delete any
`.env` file you made with `locus setup` (it can hold an API key) and remove the Locus entry from
your AI app's MCP settings. The folder also holds your policy file, a computer tag and, if you used
`locus sync-notion`, your Notion token and the Notion pages as Markdown files; remove the
integration's access in Notion too. Locus keeps no copy of your index. The downloaded embedding model is kept apart, in `~/.cache/locus/onnx`
(or the folder `FASTEMBED_CACHE_PATH` names), and deleting the data folder does not remove it.

Deleting the folder does not reach excerpts your assistant already received (they stay in that
app's conversation history), text already sent to a provider you used, copies in backups,
snapshots or synced folders, and an ordinary delete is not a secure wipe on an SSD.

Files you delete from disk, and files the deny-list now refuses, leave the index the next time you
run `locus index <folder> --prune`. There is no "forget one folder" command yet: to stop searching
a folder whose files you keep, start a fresh collection (a new `LOCUS_COLLECTION` name) and index
only the folders you want. To remove the engine itself: `uv tool uninstall locusfiles` (or
`pipx uninstall locusfiles`).

Deleting your account does not touch the files or index on your computer, and deleting your local
index does not close your account.
