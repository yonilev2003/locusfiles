---
id: assistant-did-not-use-locus
title: The assistant didn't use Locus, or the first remote question timed out
url: /docs/connect#claude-ai-web
keywords: did not use locus, didn't use locus, didn't call locus, didn't search locus, never called locus, not calling locus, ignored locus, ignores locus, said use locus, told to use locus, searched google drive instead, searched past chats, wrong tool, picks another connector, many connectors, too many connectors, tool selection, name the connector, custom instructions, project instructions, first question timed out, timed out, timeout, first question slow, slow first answer, cold start, warm up, warmup, ask again
updated: 2026-10-02
---
Two different things can happen with a web AI app that reaches Locus through the remote connection.
Neither means your files are missing from the index.

**1. The assistant answered without calling Locus.** An AI app with many connectors switched on can
answer a question about your own documents from another one, such as a cloud drive or past chats,
and never search Locus. In one test of Claude.ai with about fifteen connectors on, it did that until
it was told to use Locus; ChatGPT searched Locus without being asked. What to try today:

1. Say it in the question: "Use Locus: which two documents disagree about the launch date?"
2. Name the connector for what it holds, for example "Locus - my files" instead of "Locus", so the
   assistant can tell what it is for.
3. Add a line to the project or custom instructions, for example: "For questions about my own
   documents, notes or files, search Locus first."
4. Switch off the connectors you don't need in that chat.

The AI app decides which connector to call, so none of these is guaranteed.

**2. "Did not answer within 25 seconds" on the first question.** The first question after
`locus serve --transport relay` starts can take longer, because the search model loads the first
time it is needed and the relay waits only 25 seconds for your computer. Ask again; in our own test
the second try worked. If it keeps failing, see "The remote connection stopped or will not start".

**Locus 0.1.2 and later is built to help with both.** Its tool descriptions tell an assistant to
search Locus first for your own documents, notes and files, and `locus serve` loads the search model
and opens your index in the background as soon as it starts. Update with `uv tool upgrade locusfiles`
(or `pipx upgrade locusfiles`), then restart `locus serve --transport relay`.

What it sends: When you reach your computer from another device or a web AI app, your question and
the matching excerpts travel through Locus's relay (Vercel and Supabase) to and from your computer.
They are encrypted in transit and we don't store them.
