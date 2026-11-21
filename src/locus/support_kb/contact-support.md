---
id: contact-support
title: Get help from a person
url: /support
keywords: contact, contact us, support, help, human, person, email, telephone, call, talk to someone, ticket, report a bug, bug, feedback, complaint, response time, hours, escalate
updated: 2026-11-17
---
Locus is maintained by one person. To reach the maintainer, open an issue on the project's
repository (see CONTRIBUTING.md there); replies can take a day or two. Security problems: do not open
a public issue with details; follow SECURITY.md.

To get an answer in one round trip, include:

- the report from the `locus_help` tool with `check=true`, which never contains API keys, file
  contents or file paths, so it is safe to share; or the output of `locus doctor`, which shows your
  data folder and Python paths (they usually include your user name), so remove them if you prefer;
- which AI app you use and how you installed Locus (pipx, uv or a source checkout);
- the exact error message you see.

Never post your API key, your `.env` file or your own files in an issue. Nobody at Locus will ask for
them.

For a question about data you gave to the earlier hosted relay, see "Delete your local data (and what
about an account)".

Your AI app can also help directly, from inside the chat: `locus_help` answers questions from these
articles, and `locus_help` with `check=true` checks this computer's setup (it adds the account status
only when `LOCUS_API_KEY` is set, which ordinary use does not need).
