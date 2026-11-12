---
id: relay-troubleshooting
title: The remote connection stopped or will not start
url: /docs/connect
keywords: relay, transport relay, locus serve --transport relay, remote, remote connection, stopped, exit code, exit 1, exit 2, exit 3, superseded, another computer, took over, offline, asleep, did not answer, timeout, older engine, update, web app not working, too old, 426, LOCUS_MCP_RELAY_SUPABASE_ANON_KEY, public key
updated: 2026-10-03
---
`locus serve --transport relay` exits with a code that says what happened:

- **Exit 1:** it lost the relay channel and could not get it back, or could not refresh its token in
  time. Safe to restart.
- **Exit 2:** something restarting cannot fix: missing configuration (`LOCUS_API_KEY` not set, or
  the `relay` extra not installed; the error line gives the install command), an API key that was
  rejected or revoked, a subscription or trial that is not active, a Locus version older than the
  relay accepts ("Update Locus: ..."; update as below), the relay's public key missing
  (`LOCUS_MCP_RELAY_SUPABASE_ANON_KEY is not set`, see "Common error messages"), or the relay refused
  the channel. Fix the cause first; `locus_account_status` shows the plan state.
- **Exit 3:** superseded. Another computer started serving this account, and only one computer per
  account serves remote requests at a time. Do not set it to restart automatically on exit 3, or two
  computers keep taking over from each other.

If the web app says your computer did not answer: the computer is probably asleep, turned off,
offline, or not running `locus serve --transport relay`. Wake it, start the command, and ask again.
That message does not mean that nothing matched.

If the web app says your computer runs an older Locus engine, update Locus
(`uv tool upgrade locusfiles`, or `pipx upgrade locusfiles`) and restart `locus serve --transport relay`.

The remote connection was still in testing when this version of Locus was released; see "Claude.ai,
ChatGPT and phone apps" for its status.
