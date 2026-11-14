# Locus engine licence: questions and answers

Copyright 2026 Yonathan Levy. The whole of this repository is licensed under the PolyForm Noncommercial License
1.0.0 (`PolyForm-Noncommercial-1.0.0`); the binding text is [`LICENSE`](LICENSE).

> This page explains the licence in plain words, as the licensor reads it. It is not legal advice and it does not
> change the licence. If this page and [`LICENSE`](LICENSE) ever disagree, the LICENSE file wins. If your case is
> not clear, treat it as commercial and ask (see the last question).

## Which licence covers what?

| Part | Licence | Where the text is |
|---|---|---|
| The Locus engine: the Python package in `src/locus/`, published on PyPI as `locusfiles` | PolyForm Noncommercial License 1.0.0 | [`src/locus/LICENSE`](src/locus/LICENSE), shipped inside the wheel and the sdist |
| Everything else in this repository (tests, scripts, workflows, documentation) | The same licence | [`LICENSE`](LICENSE) at the repository root (identical to `src/locus/LICENSE`) |
| Releases before 0.2.0 (0.1.0, 0.1.3 and 0.1.4 on PyPI) | Functional Source License 1.1, ALv2 Future License (`FSL-1.1-ALv2`), the licence they were released under; it stays theirs | The `LICENSE` file inside each of those packages |
| The hosted services (the relay, the website, billing) | Not licensed to you. You use them under the Terms of Service | `/terms` on the Locus website |
| Third-party packages the engine depends on | Their own licences | `src/locus/THIRD_PARTY_LICENSES.md` and `src/locus/NOTICE` |

## What may I do with Locus?

Anything for a **noncommercial purpose**. In plain words, that is:

- **Personal use:** research, experiments and testing for the benefit of public knowledge, personal study,
  private entertainment, hobby projects and amateur pursuits, or religious observance, with no commercial
  application in mind;
- **Use by** a charitable organisation, an educational institution, a public research organisation, a public
  safety or health organisation, an environmental protection organisation, or a government institution, whatever
  the source of its funding.

Within those purposes you may use, copy, change and redistribute Locus, and make new works based on it.

## What may I not do?

Use it commercially. In plain words, that is work for a company, paid client work, or use inside a product or
service, **including when you are one person**. The licence's own words are "any noncommercial purpose is a
permitted purpose"; anything that is not a noncommercial purpose is not covered by the licence.

## Can I get a commercial licence?

**Not yet.** A paid commercial licence is planned and does not exist: none can be bought yet. If you want one,
leave your details at <https://locusfiles.com/commercial>; see [`COMMERCIAL-LICENSE.md`](COMMERCIAL-LICENSE.md).
Until it exists, do not use Locus for commercial purposes.

## Does Locus enforce this?

No. Locus has no licence key, no lock and no check that contacts a server. In a terminal, the first time you run
`locus setup` or `locus doctor`, it asks one question ("Will you use Locus for work at a company, for paid client
work, or in a product or service?") and keeps your answer in a small file in your data folder; it sends nothing.
`locus license` prints the licence in two sentences. An AI assistant connected to Locus may mention the licence once
if you tell it you use Locus for work.

## Is it open source?

No. The Open Source Definition does not allow a restriction to noncommercial use, and this licence is not
OSI-approved, so Locus never calls itself "open source". The accurate word is **source-available**. Unlike the
licence of the earlier releases, this one has no later open-source licence: a version does not turn into Apache-2.0
after some years.

## If I redistribute it, what must I keep?

You must make sure that anyone who gets a copy of any part of Locus from you also gets a copy of the licence terms
or the URL for them, and any plain-text lines that begin with `Required Notice:` that came with the software. The
one in this repository is `Required Notice: Copyright 2026 Yonathan Levy (https://locusfiles.com)`. The licence does
not let you sublicense or transfer your licence to anyone else.

## Are there patent terms?

Yes. You get a patent licence from the licensor for the patent claims the licensor can license that you would
infringe by using the software. If you make a written claim that the software infringes a patent, your patent licence
ends at once (and, if your company makes the claim, for work on its behalf).

## What happens if I break the terms?

If the licensor tells you in writing that you have broken them, your licences can continue if you come into full
compliance and take practical steps to correct past violations within 32 days of receiving the notice. Otherwise
all your licences end at once.

## Is there a warranty?

No. As far as the law allows, the software comes as is, without any warranty or condition, and the licensor is not
liable for damages. The Terms of Service govern the hosted services separately.

## I am not sure whether my use is commercial. What do I do?

Ask before you rely on it: use the form at <https://locusfiles.com/commercial>. The line is not "do you earn money
from Locus directly": work for a company or a paying client counts even when only one person uses it and Locus
is only a tool on the way. If you must decide before you get an answer, treat it as commercial and do not use it.

## Who decided this, and where is it recorded?

The licensor chose this licence on 2026-11-14, replacing the earlier FSL-1.1-ALv2 licence for version 0.2.0 and
later. The licensor and the copyright notice are named in [`LICENSE`](LICENSE) and `src/locus/NOTICE`.

## Approval

- [ ] Founder: wording of this page after the 2026-11-14 licence change has not been approved yet. It must be read
  together with the licence text, which is the official PolyForm text, unchanged.
