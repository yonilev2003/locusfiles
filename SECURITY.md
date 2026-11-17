# Security policy

Locus is a local-first tool: a local engine and MCP server (Python package `locusfiles`) that indexes
folders you choose and searches them for your MCP client.

## Reporting a vulnerability

Use GitHub's private vulnerability reporting: open the **Security** tab of this repository and choose
**Report a vulnerability**. Please do not open a public issue with details.

If that option is not available to you, open a public issue that says only that you have a security
report, with no details, and a private way to reach you will be arranged.

Include what you found, the steps to reproduce it and the impact you believe it has. Do not include
other people's personal data in the report.

## What to expect

These are intentions of a one-person project, not promises or a service-level agreement:

- Acknowledge your report within about 3 business days.
- Give you an initial assessment within about 10 business days.
- Fix confirmed issues as fast as one person reasonably can, tell you when it is fixed, and credit
  you if you wish.
- Ask you to give a reasonable chance to fix the issue before you publish details.

There is no bug-bounty program and no payment for reports.

## Scope

In scope:

- The engine and MCP server in this repository (`src/locus/`), including how it reads files, what it
  writes under `~/.locus`, and what it returns to an MCP client.
- Secrets or credentials accidentally committed to this repository.

Out of scope:

- Vendors' own services (Hugging Face, PyPI, GitHub, model and payment providers). Report those to
  the vendor.
- A user's own computer, operating system or chosen MCP client or AI assistant.
- Denial-of-service or volumetric testing, spam, and social engineering of anyone.
- Automated-scanner output with no demonstrated impact.
- Advisories in third-party dependencies that do not affect how Locus uses them (for example,
  chromadb's HTTP server, which Locus never starts; a CI rule keeps it that way).

## Known limits

These are known and described here so that you do not have to find them. A report that shows one is worse than
described here is welcome.

- **Prompt injection.** A file you index can contain text written to steer an AI assistant. Locus labels what
  it returns (chunk text, excerpts, file paths and names) as untrusted file content and its server instructions
  tell the assistant not to follow instructions found in it. That is a request to the model, not a control:
  models can be fooled and Locus cannot prevent it. Locus's tools carry the MCP read-only hint (a hint to the
  client, not enforcement; search and list-sources first update Locus's own index, and we found no code path in
  the tools that writes to an indexed file); the harm comes from other tools the same assistant has (sending, posting, opening web pages, running commands). Do not pair Locus with such
  tools for folders that hold files from other people. Whether such wording protects the maintainer in law has
  not been reviewed by a lawyer.
- **The index is readable text.** `~/.locus` holds the text of every indexed chunk, unencrypted, next to the
  vectors. It is created owner-only on macOS and Linux (not checked on Windows). Anything that can read the
  folder, including backups and synced drives, can read the text.
- **The deny-list works on names and places, not contents.** It applies to `locus index <folder>`, `locus watch`,
  `--prune` and `--roots`, not to `locus sync-notion`, and a password inside an ordinary note is indexed. A file
  indexed by an earlier version stays in the index until `locus index <folder> --prune`.
- **Hard links.** A file with more than one hard link is not indexed (its other name may be outside the folder),
  unless `LOCUS_ALLOW_HARD_LINKS=1`. A filesystem that does not report link counts cannot be checked.
- **A file swapped for a link during a read.** `locus_read_file` refuses a path that is not in the index or that
  has become a link, and checks again after reading and drops the text if the path changed. A swap that is undone
  before the second check is not detected.
- **Hosted services.** The relay and the managed-embedding endpoint run on Locus's servers, whose code is not in
  this repository. What this repository states about their behaviour (no storage of text, encryption in transit)
  is Locus's statement and cannot be checked from here.

## Please do not

- Access, change or delete data that is not yours.
- Keep or share any data you reached by accident.

## Good-faith statement

If you act in good faith, follow this policy and report promptly, I will not start legal action
against you for the research. This is a statement of intent by the maintainer. It is not a legal
safe harbor, it does not bind any third party and it does not authorize anything unlawful.

## Current security posture

No security audit, penetration test or compliance certification exists. What leaves your computer is
described in the README under "What leaves your computer".
