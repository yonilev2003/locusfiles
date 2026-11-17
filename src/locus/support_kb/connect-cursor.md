---
id: connect-cursor
title: Connect Cursor
url: /docs/connect#cursor
keywords: cursor, cursor ide, mcp.json, .cursor, cursor settings, mcp tab, agent mode, ide, editor, connect, setup
updated: 2026-11-17
---
Status: these steps were run on a pre-release engine on 2026-09-16 and have not been tested on this
version (0.2.0).

1. Create `.cursor/mcp.json` in the root of your project, or `~/.cursor/mcp.json` to have Locus in
   every project:

```json
{
  "mcpServers": {
    "locus": {
      "command": "/absolute/path/to/locus",
      "args": ["serve"]
    }
  }
}
```

   `command` must be the full path that `command -v locus` prints, not just `locus`.
2. Open Cursor Settings (the Cursor menu → Settings → Cursor Settings), go to the MCP section and check
   that "locus" shows as connected with its tools listed. Cursor's menu names change between versions.
3. Ask your question in the chat panel. If Cursor does not use the tools, try its Agent mode (mode names
   change between versions). Cursor can call the Locus tools and cite the file paths they return.

If Locus shows an error in the MCP tab, run `locus doctor` in a terminal, fix any FAIL line, then
toggle the server off and on (or restart Cursor).
