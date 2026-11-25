---
id: clients-matrix
title: Which AI apps work with Locus: the clients matrix
url: /docs/connect
keywords: clients, client, matrix, supported, compatible, works with, which assistants, claude code, claude desktop, cursor, codex, copilot cli, gemini cli, windsurf, lm studio, claude.ai, chatgpt, tested, not tested, mcp client, locus setup
updated: 2026-11-22
---
### Which assistants does it work with?
Any MCP client that can start a local server. Of the clients with setup pages, only Claude Code has a recorded run on
this version (see Works with). claude.ai and ChatGPT are not supported in this release.

"Tested" means the project ran it, not that the vendor supports it. `locus setup --client NAME` writes or prints the
configuration; `locus setup --list` lists the clients.

| Assistant | Tested by Locus |
|---|---|
| Claude Code | recorded run on 0.2.0 (automated, one Mac, 2026-11-14) |
| Claude Desktop | older record only (pre-release, 2026-09-16) |
| Cursor | older record only (pre-release, 2026-09-16) |
| Codex, Copilot CLI | earlier engine only (0.1.0, 2026-10-01) |
| Gemini CLI, Windsurf, LM Studio | not tested by Locus |
| claude.ai, ChatGPT | not supported in this release |
