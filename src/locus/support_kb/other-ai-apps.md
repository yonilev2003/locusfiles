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

- **Tested**: we saved a transcript of a cited answer through it. The records are dated and were
  made on macOS with earlier engine versions, not on this one: Claude Code, Codex CLI (its IDE
  extension and desktop panel were not tested) and GitHub Copilot CLI (with Copilot's hosted model;
  local models are not verified) on engine 0.1.0 on 2026-10-01; Claude.ai and ChatGPT on the web
  (ChatGPT on a paid plan only) through Locus's hosted relay on engine 0.1.3 on 2026-10-04 (that
  route is off by default in this version; see the article on web and phone apps). On this version
  (0.2.0) only Claude Code has a recorded run (2026-11-14); the others have not been repeated on it.
- **Older record**: Claude Desktop and Cursor ran on a pre-release engine on 2026-09-16 and have
  not been re-run on any release.
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
