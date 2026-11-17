# Clients that `locus setup --client` knows

`locus setup --client NAME` prints the MCP entry for one AI app and the file it belongs in. With `--write` it adds the `locus` entry to that file after asking, keeping a dated backup of the file first. `locus setup --list` prints this table.

**Setup writes a configuration. It does not mean Locus has been tested with that client.** The column "Tested by Locus" says what record exists; read it as that and nothing more.

| Name | Config file | Tested by Locus |
|---|---|---|
| `claude-code` (Claude Code) | none: prints a command | recorded run on 0.2.0 (2026-11-14, macOS) |
| `claude-desktop` (Claude Desktop, local servers) | macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`<br>Linux: `~/.config/Claude/claude_desktop_config.json`<br>Windows: `%APPDATA%\Claude\claude_desktop_config.json` | older record only (pre-release, 2026-09-16) |
| `cursor` (Cursor) | macOS: `~/.cursor/mcp.json`<br>Linux: `~/.cursor/mcp.json`<br>Windows: `%USERPROFILE%\.cursor\mcp.json` | older record only (pre-release, 2026-09-16) |
| `codex` (Codex CLI) | macOS: `~/.codex/config.toml`<br>Linux: `~/.codex/config.toml`<br>Windows: `%USERPROFILE%\.codex\config.toml` | earlier engine only (0.1.0, 2026-10-01) |
| `copilot` (GitHub Copilot, VS Code native and Copilot CLI) | macOS: `~/.copilot/mcp-config.json`<br>Linux: `~/.copilot/mcp-config.json`<br>Windows: `%USERPROFILE%\.copilot\mcp-config.json` | earlier engine only (0.1.0, 2026-10-01) |
| `gemini-cli` (Gemini CLI) | macOS: `~/.gemini/settings.json`<br>Linux: `~/.gemini/settings.json`<br>Windows: `%USERPROFILE%\.gemini\settings.json` | not tested |
| `windsurf` (Windsurf) | UNRESOLVED: print only | not tested |
| `lm-studio` (LM Studio) | edited in the app: print only | not tested |
| `agents-md` (AGENTS.md or CLAUDE.md text for a coding agent) | a text file you choose: print only | not tested |

## Notes

- **claude-code**: There is no file to edit by default: Claude Code keeps its own list. Run the printed command yourself; Locus never runs `claude`. Inside Claude Code, `/mcp` lists the servers it has. The recorded run is automated Claude Code sub-agents on one Mac (2026-11-14), not an interactive session. Vendor pages (read 2026-11-17): https://code.claude.com/docs/en/mcp.
- **claude-desktop**: On Linux the folder is $XDG_CONFIG_HOME/Claude when that variable is set, else ~/.config/Claude. Vendor pages (read 2026-11-17): https://modelcontextprotocol.io/docs/develop/connect-local-servers; https://support.claude.com/en/articles/10949351-getting-started-with-local-mcp-servers-on-claude-desktop.
- **cursor**: This is the global file; a project can have its own .cursor/mcp.json, which this command does not touch. Vendor pages (read 2026-11-17): https://cursor.com/docs/mcp.
- **codex**: Per the vendor page, the Codex IDE extension and the ChatGPT desktop app read the same file. Only the CLI has an earlier record; the others were not tested. The vendor page also offers `codex mcp add NAME -- command args`. Vendor pages (read 2026-11-17): https://learn.chatgpt.com/docs/extend/mcp.
- **copilot**: VS Code and Copilot CLI share this file. The record is for Copilot CLI only (version 1.0.86, with Copilot's hosted model); VS Code with GitHub Copilot: not tested. The file is $COPILOT_HOME/mcp-config.json when COPILOT_HOME is set, else ~/.copilot/mcp-config.json. The older .vscode/mcp.json is deprecated and is not written. Only command, args and env are written. Copilot CLI 1.0.96 reads such an entry as a local server with all tools (checked with `copilot mcp get locus` in a scratch folder on 2026-11-17: that shows how the file is read, not a connection). Vendor pages (read 2026-11-17): https://code.visualstudio.com/docs/agents/reference/mcp-configuration; https://docs.github.com/en/copilot/how-tos/copilot-cli/customize-copilot/add-mcp-servers.
- **gemini-cli**: Google's docs banner says Gemini CLI was replaced by Antigravity CLI for unpaid and Google One users on 2026-06-18. If that is you, Gemini CLI may no longer be the tool you run. Antigravity CLI is a different tool with its own file, which this command does not write. settings.json holds other Gemini CLI settings too; only the locus entry is added or changed. Vendor pages (read 2026-11-17): https://geminicli.com/docs/tools/mcp-server.
- **windsurf**: UNRESOLVED config path, so this client is print-only. VS Code's MCP reference lists ~/.codeium/windsurf/mcp_config.json; Windsurf's own page lists the Devin-style paths .devin/mcp_config.json and ~/.config/devin/mcp_config.json. Locus does not know which one your version reads: check in Windsurf, then paste the snippet into that file. Vendor pages (read 2026-11-17): https://docs.windsurf.com/windsurf/cascade/mcp; https://code.visualstudio.com/docs/agents/reference/mcp-configuration.
- **lm-studio**: Print-only: LM Studio edits its mcp.json from the app (Program tab, Install, Edit mcp.json). Paste the snippet there. The snippet uses the common mcpServers shape; check it against LM Studio's page when you paste it. Vendor pages (read 2026-11-17): https://lmstudio.ai/docs/app/mcp.
- **agents-md**: The --json flag of `locus query` requires Locus 0.2.0 or later.

## What `--write` does

- Shows the file and the change, with secret values hidden, and asks (`--yes` skips the question).
- Copies the file to `<file>.locus-backup-YYYYMMDDTHHMMSS`, then replaces it through a temporary file.
- Adds or replaces only the `locus` entry. A file that does not parse is left alone; the snippet is printed.
- Running it twice changes nothing the second time. `--remove --write` removes only the `locus` entry.
- `--env NAME` adds an env entry whose value is read from your environment when writing; it is never printed.
