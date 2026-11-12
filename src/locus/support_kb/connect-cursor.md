---
id: connect-cursor
title: Connect Cursor
url: /docs/connect#cursor
keywords: cursor, cursor ide, mcp.json, .cursor, cursor settings, mcp tab, agent mode, ide, editor, connect, setup
updated: 2026-10-01
---
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
2. Open Cursor Settings (`Cmd+Shift+J`, or the Cursor menu → Settings → Cursor Settings), go to the
   **MCP** tab and check that "locus" shows as connected with its tools listed.
3. Use Agent mode (not Ask) in the chat panel and ask your question. Cursor calls the Locus tools
   for you and can cite the file paths they return.

If Locus shows an error in the MCP tab, run `locus doctor` in a terminal, fix any FAIL line, then
toggle the server off and on (or restart Cursor).
