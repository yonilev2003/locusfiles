---
id: ai-app-cannot-see-locus
title: My AI app does not show Locus or its tools
url: /support
keywords: tools missing, no tools, not showing, not listed, not appearing, cannot see, can't see, disconnected, not connected, failed, failed to start, server failed, spawn, enoent, command not found, no such file, mcp server, config, json, restart, path, absolute path, PATH, red, error, broken, doesn't show, does not show, not shown, tools icon, hammer, mcp tab, claude desktop, cursor, claude code
updated: 2026-10-02
---
This is almost always the AI app's MCP config, not Locus itself. Check, in this order:

1. **The command path.** `command` must be the full path that `command -v locus` prints, with
   `serve` as its only argument. A bare `locus` or a relative path fails because desktop apps don't
   see your terminal's PATH; the app then reports errors such as "ENOENT", "spawn ... failed" or
   "command not found".
2. **A full restart.** Quit the app completely and reopen it; most apps read their MCP config only
   at startup.
3. **Valid JSON.** Add `locus` inside the existing `mcpServers` object rather than pasting a second
   top-level object, and check commas and braces.
4. **The current config shape.** Some apps changed their config file between versions. Use the
   app's entry on the connect page, not an old snippet.
5. **Settings the app can see.** If Locus starts but says it is not configured, the app is not
   reading your `.env` (it starts Locus from its own folder). Put `LOCUS_EMBEDDER`, `LOCUS_API_KEY`
   or provider keys in the server's `env` block in the app's config. `locus setup --managed` (or
   `--local`) prints these settings for Claude Desktop, Cursor and Claude Code, ready to paste
   (with a plan, put your key where it says `<your key>`).
6. **Locus itself.** Run `locus doctor` in a terminal and fix every FAIL line.

On macOS, the first time your AI app reads a file through Locus from Desktop, Documents or Downloads,
macOS asks for permission; allow it.
