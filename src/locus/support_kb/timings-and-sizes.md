---
id: timings-and-sizes
title: Timings and sizes: install time, indexing speed, disk and download size
url: /support
keywords: how long, slow, speed, time, timing, timings, fast, seconds, minutes, install time, setup time, indexing speed, chunks per second, download size, 530 MB, 134 MB, disk, disk space, size, estimate, locus estimate, large folder, benchmark, measured, cold cache, apple silicon
updated: 2026-11-22
---
### How long does it take to set up?
About three minutes on a fast connection, plus indexing time that depends on the folder: about 8 chunks per second in
one measurement. Details and the measurement conditions are under Install.

**Measured once on one Apple-silicon Mac, with a cold package cache and no cached model, on 2026-11-18** (power
source not recorded):

- about 33 s to install from PyPI;
- about 95 s for the first `locus doctor`, including the 134 MB model download;
- about 8 chunks per second while indexing (28 markdown files, 117 chunks, about 15 s, including start-up).

These are single measurements. A large folder takes much longer, and slower networks take longer. Run
`locus estimate <folder>` first to see the size, and start with a small folder.

**Sizes.** About 530 MB in total to download: the environment plus the 134 MB model, which is stored in
`~/.cache/locus/onnx`, outside `~/.locus`.

**Not measured:** latency and index size at scale, and performance on code search.
