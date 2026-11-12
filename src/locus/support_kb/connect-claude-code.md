---
id: connect-claude-code
title: Connect Claude Code
url: /docs/connect#claude-code
keywords: claude code, claude cli, cli, terminal, claude mcp add, mcp add, scope user, /mcp, connect, setup
updated: 2026-10-02
---
Register Locus once for every project (`--scope user`), using the full path that `command -v locus`
prints:

```bash
claude mcp add --scope user locus -- /absolute/path/to/locus serve
claude      # start Claude Code in any folder
/mcp        # inside Claude Code: "locus" should show Connected
```

To pass settings such as a managed-plan key, add them with `-e`:
`claude mcp add --scope user locus -e LOCUS_EMBEDDER=managed -e LOCUS_API_KEY=<your key> -- /absolute/path/to/locus serve`.
`locus setup --managed` (or `--local`) prints a `claude mcp add` line with your settings and the
full path filled in (put your key where it says `<your key>`); add `--scope user` to it to register
Locus for every project.

If `/mcp` shows Locus as failed, run `locus doctor` in a terminal and fix any FAIL line, then
restart Claude Code. Developers working inside a Locus source checkout: its `.mcp.json` already
registers Locus when you run `claude` from the checkout with its `.venv` activated.
