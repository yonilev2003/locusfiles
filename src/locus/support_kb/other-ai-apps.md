---
id: other-ai-apps
title: Connect other AI apps and coding agents
url: /docs/connect
keywords: other apps, other clients, supported apps, compatible, which apps, cline, copilot, github copilot, codex, gemini cli, opencode, openclaw, kimi, qwen, deepseek, lm studio, goose, open webui, anythingllm, kilo code, raycast, 5ire, oterm, ide, cli, agent, mcp client, tested
updated: 2026-10-02
---
Any MCP client that can start a local server runs Locus the same way: the command is the full path
of `locus` (from `command -v locus`) and the only argument is `serve`. Locus then talks to the app
over standard input and output (stdio).

The connect page has step-by-step setup for each client, labelled by how far we have tested it:

- **Tested**: we recorded a cited answer through it. When this version of Locus was released, that
  meant Claude Desktop, Claude Code, Cursor, Codex (the Codex CLI; its IDE extension and desktop
  panel are not tested yet), GitHub Copilot CLI (with Copilot's hosted model; local models are
  not verified) and ChatGPT on the web (through Locus's hosted relay, connecting with an OAuth
  sign-in, tested on a paid plan and not on the free plan; see the article on web and phone apps).
- **Connects** and **Documented**: not yet verified. Connects means we saw it connect; Documented
  means the steps come from the vendor's own documentation.

Things that trip people up:

- Several clients changed their config file shape between versions. Use the client's current entry
  on the connect page, not an old snippet.
- Some clients prefix tool names, so `locus_search` can appear as `locus__locus_search` or
  `mcp__locus__locus_search`. That is normal.
- Goose needs an LLM provider configured before it loads any MCP extension.
- If the client starts Locus from its own working directory, a `.env` file elsewhere is not read:
  put settings in the client's `env` block for the server instead.
