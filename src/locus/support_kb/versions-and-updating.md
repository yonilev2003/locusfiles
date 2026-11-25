---
id: versions-and-updating
title: Versions, platforms, updating and licence
url: /support
keywords: version, versions, update, updating, upgrade, pre-release, rc, 0.2.0, 0.2.0rc2, 0.1.4, pypi, uv tool, pipx, install version, windows, linux, mac, macos, platform, free, licence, license, open source, source-available, polyform, commercial, changelog
updated: 2026-11-22
---
**Versions.** `0.2.0rc2` is a pre-release on PyPI and the PyPI default is still the older 0.1.4, so name the version:
`uv tool install --python 3.12 --prerelease allow "locusfiles[local]==0.2.0rc2"`.

**Updating.** `uv tool upgrade locusfiles` (or `pipx upgrade locusfiles`); restart the AI app afterwards. Embedding
model or chunk-size changes need a new collection.

### Does it work on Windows or Linux?
It was tested on one Mac. The install-to-answer journey (17 checks) also passed on GitHub's Ubuntu and Windows runners (x86_64, one run on
2026-11-18) and in a Debian arm64 container. About 200 of the repository's own tests fail on Windows and have not
been sorted yet, so Windows is not verified as supported.

### Is Locus free?
Free for noncommercial use under the PolyForm Noncommercial License 1.0.0. Work at a company, paid client work and use
inside a product or service need a separate commercial licence, which cannot be bought yet:
<https://locusfiles.com/commercial>.

### Is it open source?
No. It is source-available: you can read and modify the code under the licence, and the licence limits commercial use.
