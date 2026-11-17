---
id: connect-claude-code
title: Connect Claude Code
url: /docs/connect#claude-code
keywords: claude code, claude cli, cli, terminal, claude mcp add, mcp add, scope user, /mcp, connect, setup
updated: 2026-11-17
---
Register Locus once for every project (`--scope user`), using the full path that `command -v locus`
prints:

```bash
claude mcp add --scope user locus -- /absolute/path/to/locus serve
claude      # start Claude Code in any folder
/mcp        # inside Claude Code: "locus" should show Connected
```

To pass a setting to the server, such as a collection name, add it with `-e`:
`claude mcp add --scope user locus -e LOCUS_COLLECTION=<name> -- /absolute/path/to/locus serve`.
`locus setup --local` prints a `claude mcp add` line with your settings and the full path filled in;
add `--scope user` to it to register Locus for every project.

If `/mcp` shows Locus as failed, run `locus doctor` in a terminal and fix any FAIL line, then
restart Claude Code.
