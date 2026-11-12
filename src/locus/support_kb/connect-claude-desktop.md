---
id: connect-claude-desktop
title: Connect Claude Desktop
url: /docs/connect#claude-desktop
keywords: claude desktop, claude app, desktop app, claude_desktop_config.json, config, edit config, developer settings, mcp, mcpservers, connect, setup, tools menu
updated: 2026-10-01
---
1. Find the full path of the `locus` command: run `command -v locus` in a terminal. After a pipx or
   uv install it is usually `~/.local/bin/locus` written out in full, such as
   `/Users/you/.local/bin/locus` on macOS.
2. In Claude Desktop open **Settings → Developer → Edit Config**. This opens
   `claude_desktop_config.json`.
3. Add Locus. If the file already has content, merge this into it rather than replacing it:

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

   Use the full path from step 1 as `command`, not just `locus`: desktop apps don't see your
   terminal's PATH. Settings such as `LOCUS_EMBEDDER` or `LOCUS_API_KEY` go in an `"env": { ... }`
   block next to `"args"`, because Claude Desktop does not start Locus in the folder that holds your
   `.env` file.
4. Quit Claude Desktop completely (not just the window) and reopen it. The config is read only at
   startup.
5. Start a new chat, open the tools menu below the message box and check that "locus" is listed.
   Then ask a question that needs your files: Claude calls Locus and cites the file paths it used.
