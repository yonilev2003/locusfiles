# Using Locus from a cloud assistant through a tunnel

Most people never need this page. A program that runs on your own computer (Claude Code, Claude Desktop, Codex, Cursor and others) can start Locus over a private connection on your machine, with no address on the internet; `docs/clients.md` says what has been tested with each. Read on only if you want an assistant that runs in a company's cloud (for example a chat website) to ask Locus questions about your files.

This page describes what Locus does in this mode and how to start it. It does not promise that any particular assistant will accept the connection. The table near the end says, client by client, what has been tested. Today that is almost nothing.

## What a tunnel is

Claude and ChatGPT run on their companies' servers. They cannot reach a program on your laptop. A tunnel is a small helper program that gives your laptop a temporary public web address and passes the requests that arrive there on to Locus. The assistant calls that address; the tunnel hands the call to Locus; Locus answers from your index.

## Read this before you start

- **Excerpts leave your computer.** When the assistant searches, Locus sends back text from your files: excerpts of up to 19,500 characters per search, file paths, and the full text of any file the assistant asks to read. That text goes to the assistant's provider and is handled under that provider's own rules. Indexing can stay on your computer; this part cannot. Locus never claims that nothing leaves your machine in this setup.
- **Whoever runs the tunnel carries the traffic.** Tailscale Funnel is built so that Tailscale's relay cannot read the traffic; the TLS connection ends on your own device. With Cloudflare Tunnel and ngrok the company's edge ends the TLS connection and can see the requests and answers in the clear, by design of how those products work. Locus does not control any of this.
- **The address is public.** Anyone who finds it can knock on it. The only things that keep strangers out are the secret token (below) and the limits Locus puts in front of it. Treat the token like a password.
- **A file can try to steer the assistant.** Locus labels everything it returns as untrusted file content, but a label cannot stop every attempt. If the same chat also has a connector that can send mail or write files, text inside one of your files could try to push content there. Use Locus alone for sensitive questions and keep the assistant's per-tool approval switched on.
- **This build uses one fixed secret token, not OAuth.** Some assistants accept a fixed token (an "authorization header"); others only accept a sign-in flow (OAuth) and Locus does not have one yet. So a client may refuse to connect even when everything on this page is right. See the table below.

## What Locus does differently in this mode

You turn it on by telling Locus the public host name (`LOCUS_MCP_PUBLIC_HOST` or `--public-host`). Without it, `locus serve --transport streamable-http` behaves exactly as before: it listens on your computer only and only accepts local host names.

With a public host name set, Locus:

| What | How it works |
|---|---|
| Still listens on 127.0.0.1 only | The tunnel program runs on your computer and connects to it. Locus refuses `--host 0.0.0.0` in this mode. |
| Accepts the public host name and no other | The protection against DNS-rebinding stays on. A request for any other host name gets `421`. A browser-style `Origin` header must be `https://<your host>`; a request with no `Origin` header (cloud servers send none) is fine. |
| Needs a token of at least 32 characters | It refuses to start with a shorter one and never prints the token. Every request must carry `Authorization: Bearer <token>`. |
| Answers in plain JSON and keeps no sessions | Cloudflare quick tunnels cannot carry Server-Sent Events. A `GET` request (which would open an event stream) is answered with `405`. `--sse` switches back to event streams and sessions. |
| Shows only read-only tools | `locus_search`, `locus_list_sources`, `locus_read_file`, `locus_help`, plus `search` and `fetch` (ChatGPT's names for the same job). `locus_diagnose_connection` and `locus_account_status` are not listed and cannot be called. `--all-tools` puts them back. Set `LOCUS_CHATGPT_TOOLS=0` to leave out `search` and `fetch`. |
| Limits the request rate | 120 requests a minute per token by default (`LOCUS_MCP_RATE_PER_MIN`), then `429` with a `Retry-After` header. Ten failed token attempts a minute from one address get `429` too. |
| Writes a counts-only log | One line per request in `audit/` inside your Locus data folder (`~/.locus/audit/` by default), readable only by you: time, method, tool name, status, number of results, bytes returned and the indexed paths returned. Never the question, never any text from your files, never the token. |
| Has an off switch | While a file named `REMOTE_OFF` exists in `~/.locus` (or in the folder `LOCUS_DATA_DIR` names), every request gets `503`. `locus pause` (or `LOCUS_DISABLED=1`) also stops the file tools in this mode: they refuse with the fixed paused sentence, and `locus resume` undoes it. |
| Expires | After 8 hours every request gets `401` and you restart Locus to continue. `--expires 2h` changes the time, `--expires 0` turns the expiry off. |

`locus_read_file` and `fetch` only read files that are already in the index. Nothing else on your computer can be reached through them.

## The steps, for every option

1. Install Locus and index a folder: `locus index <folder>`. `locus stats` must show chunks.
2. Make a secret token and keep it somewhere private:
   `python -c "import secrets; print(secrets.token_urlsafe(32))"`
   Put it in the `.env` file Locus reads, as `LOCUS_MCP_TOKEN=<the token>`.
3. Start a tunnel (below). It prints a web address that starts with `https://`. Write down the host name, which is the part between `https://` and the next `/`.
4. Start Locus with that host name:
   `LOCUS_MCP_PUBLIC_HOST=<host name> locus serve --transport streamable-http`
   (or `locus serve --transport streamable-http --public-host <host name>`). Locus prints the connector address, which is `https://<host name>/mcp`, and the tools it exposes. It does not print the token.
5. Check it from your own computer before you involve an assistant:
   `locus doctor --remote https://<host name>/mcp`
   It reads the token from `LOCUS_MCP_TOKEN` (or from the variable you name with `--token-env NAME`) and prints one OK or FAIL line per check, with the fix: the name resolves, the secure connection works, the port is 443, a request without the token is refused, a request with the token gets a JSON answer, and only read-only tools are listed.
6. Add the address to the assistant as a custom connector or custom MCP server, as that assistant's own help pages describe, and give it the token if it asks for a header or key.
7. When you are done: stop the tunnel (Ctrl+C), stop Locus, remove the connector in the assistant, and put a new token in `.env` before the next time.

To stop everything from another terminal in a hurry, create the off switch: `touch ~/.locus/REMOTE_OFF`. Delete the file to allow requests again.

## The three tunnels

These were read from each vendor's documentation on 2026-11-17. The steps below have not been run end to end with Locus.

| | Stable address | Account needed | Who can read the traffic | Notes |
|---|---|---|---|---|
| **Cloudflare Tunnel** | Yes with a named tunnel and a domain in your Cloudflare account; a quick tunnel gets a new `trycloudflare.com` address at every start | Quick tunnel: none. Named tunnel: a Cloudflare account and a domain | Cloudflare can see it | The quick tunnel is for tests: no uptime promise, no event streams (Locus's default JSON mode is made for this) |
| **Tailscale Funnel** | Yes (`<machine>.<tailnet>.ts.net`) | A Tailscale account (a beta feature) | Tailscale's relay cannot decrypt it | Port 443 is allowed; the host name contains your device and network names |
| **ngrok** | Yes: one free fixed address per account | A free ngrok account | ngrok can see it | Free plan limits: about 1 GB of traffic and 20,000 requests a month; each question costs several requests |

### Cloudflare Tunnel, quick (for a first test)

1. Install `cloudflared`.
2. Run `cloudflared tunnel --url http://127.0.0.1:8000`.
3. Copy the `https://<random>.trycloudflare.com` address it prints. The host name is the random part plus `.trycloudflare.com`.
4. Do the common steps 4 to 7. The address changes every time the tunnel starts, so a connector made yesterday no longer works.

Do not add Cloudflare's email-code protection: it needs a person in a browser, so an assistant cannot pass it.

### Cloudflare Tunnel, named

1. In your Cloudflare account, create a tunnel (Networking, Tunnels) and run the installer command it shows for your system.
2. Add a public host name such as `locus.<your domain>` that points to `http://localhost:8000`.
3. Use `locus.<your domain>` as the host name, and `https://locus.<your domain>/mcp` as the address.

An extra Cloudflare access rule that requires special headers will block assistants that cannot send them. If you add a firewall rule, make it allow the assistant company's published address ranges, and keep the token: an address range names the company, not you.

### Tailscale Funnel

1. Create a Tailscale account and install the Tailscale app. On a Mac use the version Tailscale's own pages say supports Funnel.
2. Turn on MagicDNS and HTTPS certificates in the Tailscale admin page.
3. Run `tailscale funnel 8000`. It prints `https://<machine>.<tailnet>.ts.net`.
4. Use that host name and the address `https://<machine>.<tailnet>.ts.net/mcp`. Claude's cloud connects on port 443 only, which Funnel offers.
5. Stop with Ctrl+C.

### ngrok

1. Create a free ngrok account, install ngrok, and add your auth token as the ngrok dashboard shows.
2. Run `ngrok http 8000`. It uses your free fixed address (`<name>.ngrok-free.app`).
3. Use that host name and the address `https://<name>.ngrok-free.app/mcp`.
4. Watch the monthly numbers on ngrok's usage page.

If ngrok does not pass your public host name on to Locus, `locus doctor --remote` shows a `421` and tells you which setting to check.

## What each assistant says, and what has been tested

"Not tested" means what it says: Locus has not been run against that assistant through a tunnel, so nothing here should be read as "it works". The "What its documentation says" column is the vendor's own description as read on 2026-11-17 and may have changed.

| Client | Needs a tunnel? | What its documentation says about connecting | Tested with Locus through a tunnel |
|---|---|---|---|
| Claude Code | No: it runs on your computer, so it can use Locus's normal local connection | Not checked for this page | Not tested through a tunnel. Locus's help article on other apps lists one recorded Claude Code run on Locus 0.2.0 (2026-11-14) over the normal local connection, which is not this mode |
| Claude Desktop, custom connector | Yes: Claude's cloud makes the call, on port 443 | Sign-in (OAuth) or "no sign-in"; fixed request headers are described as available only to some organizations, so check in the app | Not tested |
| claude.ai (web) and Claude on phones | Yes | As above | Not tested |
| ChatGPT (custom MCP server, web) | Yes | Sign-in (OAuth) or no authentication; a fixed token is not offered. Locus has no OAuth yet | Not tested |
| Codex CLI and the ChatGPT desktop app's Codex | No: runs on your computer | Accepts a fixed token from an environment variable | Not tested through a tunnel |
| OpenAI and Anthropic developer APIs (remote MCP tools) | Yes | A token field per server | Not tested |
| Gemini CLI | No | Accepts custom headers | Not tested through a tunnel |
| Cursor | No for the desktop app | Accepts custom headers | Not tested through a tunnel |
| GitHub Copilot cloud agent | Yes | Fixed values only; sign-in servers are not supported | Not tested |
| Devin, Mistral, Perplexity, Copilot Studio | Yes | Fixed token or key options are described | Not tested |
| Everything else in the compatibility record | Varies | See the record | Not tested |

If a client says it needs a sign-in flow (OAuth), this build cannot serve it yet.

## If something goes wrong

Run `locus doctor --remote https://<host name>/mcp`. Common results:

- `421`: Locus does not know the host name the tunnel presents. Start it with `LOCUS_MCP_PUBLIC_HOST` set to exactly that host name.
- `403`: something in front of Locus (a firewall or access rule) blocks the request. The assistant's servers would be blocked too.
- `401` with the right token: the token differs from the one the server holds, or the 8-hour expiry has passed. Restart Locus.
- `503`: the `REMOTE_OFF` file exists. Delete it.
- `429`: too many requests or too many failed attempts. Wait a minute.
- The answer comes back as an event stream: you started Locus with `--sse`; start it without.

## What has not been verified

- No assistant has been connected to Locus through a tunnel for this build.
- The failed-attempt counter groups requests by the last address in the `X-Forwarded-For` header when the request comes from your own computer, which is what a tunnel program looks like. Whether each tunnel product sets that header the way this assumes has not been checked.
- ChatGPT's search and fetch tools return a `file://` address for each file, because the format only requires a non-empty address. Whether ChatGPT shows or opens such an address is not verified.
