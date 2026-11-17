---
id: web-and-phone-apps
title: Claude.ai, ChatGPT and phone apps (remote connection)
url: /docs/connect#claude-ai-web
keywords: claude.ai, claude web, chatgpt, chat gpt, web app, browser, phone, mobile, iphone, ios, android, remote, remote connection, relay, transport relay, connector, custom connector, oauth, another device, away from computer
updated: 2026-11-17
---
Web and phone AI apps cannot start a program on your computer, and Locus offers no hosted service for
them in this version. An earlier version reached them through a hosted relay: the computer that holds
your index runs `locus serve --transport relay` and connects out to the relay, with no open port and no
tunnel. That relay mode is off by default (it refuses to run unless `LOCUS_ENABLE_HOSTED=1` is set) and
still in testing.

**Status:** the remote connection has not been re-run on this version (0.2.0). There is one run of each
on an earlier engine (0.1.3, 2026-10-04). ChatGPT on the web (on a paid plan; the free plan was not
tested) passed our cited-answer test through it; it connects with an OAuth sign-in. Claude.ai on the web
connected but did not use Locus until it was told to. The remote connection is not confirmed working
end to end: the full lifecycle run (edits, deletions, the computer going offline, removal) has not been
done, and the ChatGPT phone and desktop apps were not tested. Until then, use Claude Code (the only
client with a recorded run on this version), Claude Desktop or Cursor on the computer that has your
files.

If you turn the relay on anyway, it needs: the `relay` extra
(`uv tool install --python 3.12 "locusfiles[relay]"`, or `pipx install "locusfiles[relay]"`), a
hosted-relay account, and that account's `LOCUS_API_KEY` on the computer. Start the relay with the same
embedder you indexed with (`LOCUS_EMBEDDER=managed` if you used managed embeddings). Only one computer
per account serves remote requests at a time; starting Locus relay on another computer takes over. The
computer must be on, awake and running Locus.

If the web app reports that your computer did not answer, wake or open that computer, make sure
`locus serve --transport relay` is still running on it, and try again. "Did not answer" does not
mean that nothing matched.

What it sends: When you reach your computer from another device or a web AI app, your question and
the matching excerpts travel through Locus's relay (Vercel and Supabase) to and from your computer.
Locus says they are encrypted in transit and not stored; that is behaviour of its hosted servers,
which the engine's code cannot show. To connect, the engine also sends your account key, the Locus
version and a random 8-character computer tag (plus the label in `LOCUS_DEVICE_LABEL`, if you set
one), so that two computers do not both answer. Checked against the engine's code on 2026-11-14.
