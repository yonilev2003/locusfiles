# Locus engine licence: questions and answers

Copyright 2026 Yonathan Levy. The whole of this repository is licensed under `FSL-1.1-ALv2`; the binding
text is [`LICENSE`](LICENSE).

> This page explains the licence in plain words. It is not legal advice and it does not change the
> licence. If this page and [`src/locus/LICENSE`](src/locus/LICENSE) ever disagree, the LICENSE file wins.

## Which licence covers what?

| Part | Licence | Where the text is |
|---|---|---|
| The Locus engine: the Python package in `src/locus/`, published on PyPI as `locusfiles` | Functional Source License, Version 1.1, ALv2 Future License (`FSL-1.1-ALv2`) | [`src/locus/LICENSE`](src/locus/LICENSE), shipped inside the wheel and the sdist |
| Everything else in this repository (tests, scripts, workflows, documentation) | The same licence, `FSL-1.1-ALv2` | [`LICENSE`](LICENSE) at the repository root (identical to `src/locus/LICENSE`) |
| The hosted services (the relay, the website, billing) | Not licensed to you. You use them under the Terms of Service | `/terms` on the Locus website |
| Third-party packages the engine depends on | Their own licences | `src/locus/THIRD_PARTY_LICENSES.md` and `src/locus/NOTICE` |

## What may I do with the engine?

Anything the licence calls a **Permitted Purpose**, which is any purpose other than a Competing Use.
The licence names some examples on purpose:

- use it yourself or inside your company (internal use and access);
- use it for non-commercial education or non-commercial research;
- use it while providing professional services to someone who is themselves using it under the
  licence.

Within those purposes you may use, copy, modify, create derivative works, publicly perform,
publicly display and redistribute it.

## What may I not do?

A **Competing Use**: making the engine available to others in a commercial product or service
that (1) substitutes for the engine, (2) substitutes for another product or service the licensor
offers using the engine and that existed when that engine version was made available, or (3)
offers the same or substantially similar functionality.

## Is the engine open source?

Not in the sense of the Open Source Definition: the Competing Use restriction is not allowed in an
OSI-approved licence, so Locus never calls the engine "open source". The accurate word is
**source-available**. Each version becomes available under the Apache License, Version 2.0 two
years after that version was made available (the licence's "Grant of Future License"), and from
then on that version may be used under Apache-2.0.

## If I redistribute it, what must I keep?

The licence terms apply to every copy, modification and derivative. Include a copy of or a link to
the licence, and keep the copyright notices. The licence gives no right to use Locus's names or
marks, except to identify where the software came from.

## Are there patent terms?

Yes. For a Permitted Purpose, the grant includes a licence under the licensor's patents that the
use would necessarily infringe. That patent licence ends at once for anyone who claims that the
software infringes a patent.

## Is there a warranty?

No. The software is provided "as is", and the licence excludes liability. The Terms of Service
govern the hosted services separately.

## Who decided this, and where is it recorded?

The licence was chosen by the founder (2026-09-26) and confirmed for the whole public repository
on 2026-11-11. The licensor and the copyright notice are named in
[`src/locus/LICENSE`](src/locus/LICENSE) and `src/locus/NOTICE`.

## Approval

- [x] Founder: wording approved (2026-11-13).
