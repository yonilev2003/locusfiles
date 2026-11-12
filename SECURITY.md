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
