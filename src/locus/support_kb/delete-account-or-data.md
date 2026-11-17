---
id: delete-account-or-data
title: Delete your local data (and what about an account)
url: /privacy
keywords: delete, deletion, delete account, remove, erase, close account, account, my data, personal data, export, copy of my data, correct, correction, right to be forgotten, uninstall, local index, wipe, clear index, remove folder, forget
updated: 2026-11-17
---
**Accounts.** Locus offers no hosted service in this version, so there is no account to delete for
ordinary use. The earlier hosted relay (managed embeddings and the remote connection) is off by default
and still in testing; if you turned it on, switch it off (unset `LOCUS_ENABLE_HOSTED` and
`LOCUS_API_KEY`). What a hosted server kept is not in this package and cannot be shown or deleted from
here. For a question about data you gave to the hosted relay, open an issue on the project's repository
that says only that you have a data request, with no personal details; a private way to reach you will
be arranged.

**Your local data.** Your local index is yours to manage. The index holds the text of every chunk
Locus made from your files, as readable, unencrypted text next to the vectors, in your data
directory (default `~/.locus`, or the folder `LOCUS_DATA_DIR` names). To erase it: quit your AI app
and stop any `locus serve` or `locus watch`, delete that folder (`rm -rf ~/.locus`), delete any
`.env` file you made with `locus setup` (it can hold an API key) and remove the Locus entry from
your AI app's MCP settings. The folder may also hold your policy file, the audit log (paths and counts,
never file text), a computer tag (only if you used the remote connection) and, if you used
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

Deleting your local index does not touch anything a hosted server may have kept if you turned the earlier
hosted relay on.
