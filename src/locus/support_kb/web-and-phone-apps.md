---
id: web-and-phone-apps
title: Claude.ai, ChatGPT and phone apps (remote connection)
url: /docs/connect#claude-ai-web
keywords: claude.ai, claude web, chatgpt, chat gpt, web app, browser, phone, mobile, iphone, ios, android, remote, remote connection, relay, transport relay, connector, custom connector, oauth, another device, away from computer
updated: 2026-11-14
---
Web and phone AI apps cannot start a program on your computer, so they reach Locus through Locus's
hosted relay instead: the computer that holds your index runs `locus serve --transport relay` and
connects out to the relay, with no open port and no tunnel.

**Status:** the remote connection is in testing, and it has not been re-run on this version (0.2.0).
ChatGPT on the web (on a paid plan; the free plan was not tested) passed our cited-answer test through
it on engine 0.1.3 on 2026-10-04 (it connects with an OAuth sign-in, and the connect page has the
steps), but the remote connection
is not yet confirmed working end to end: the full lifecycle run (edits, deletions, the computer
going offline, removal) has not been done, and the ChatGPT phone and desktop apps were not tested.
Claude.ai on the web connected too but did not use Locus unless it was told to, so it is not listed
as tested yet. The connect page always has the current status and the tested steps. Until your web
app is listed as tested there, use Claude Desktop, Claude Code or Cursor on the computer that has
your files.

What the remote connection needs: the `relay` extra
(`uv tool install --python 3.12 "locusfiles[relay]"`, or `pipx install "locusfiles[relay]"`), a
Locus account, and that account's `LOCUS_API_KEY` on the computer. Start the relay with the same
embedder you indexed with (`LOCUS_EMBEDDER=managed` for a Locus account). Only one computer per account
serves remote requests at a time; starting Locus relay on another computer takes over. The computer must be
on, awake and running Locus. The remote connection is off by default: it refuses to run unless `LOCUS_ENABLE_HOSTED=1` is set, and it needs an active Locus account.

If the web app reports that your computer did not answer, wake or open that computer, make sure
`locus serve --transport relay` is still running on it, and try again. "Did not answer" does not
mean that nothing matched.

What it sends: When you reach your computer from another device or a web AI app, your question and
the matching excerpts travel through Locus's relay (Vercel and Supabase) to and from your computer.
Locus says they are encrypted in transit and not stored; that is behaviour of its hosted servers,
which the engine's code cannot show. To connect, the engine also sends your account key, the Locus
version and a random 8-character computer tag (plus the label in `LOCUS_DEVICE_LABEL`, if you set
one), so that two computers do not both answer. Checked against the engine's code on 2026-11-14.
