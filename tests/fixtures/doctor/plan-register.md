# Fixture plan with an Appendix A register (tests/test_doctor.py)

## Ch. A

> **[BLOCKER:LAUNCH] OPS-011** — Voyage carded (owner: Founder; due: 2026-09-21)

> **[BLOCKER:SCALE] UX-009** — Desktop app, unsigned beta (owner: Claude; due: 2026-11-01)

> **[BLOCKER:ENTERPRISE] UX-009** — Signed builds before the first enterprise pilot (owner: Founder; due: LATER)

> **[BLOCKER:LAUNCH] FIN-001** — Blended mix target (owner: Founder; due: 2026-09-22)

> **[BLOCKER:LAUNCH] LEG-003** — Terms billing clause — CLOSED 2026-09-23 (owner: Claude; due: 2026-09-28)

> **[BLOCKER:LAUNCH] SEC-019** — is_org_* helpers locked down (owner: Codex; due: 2026-09-27)

> **[BLOCKER:W2] RET-001** — Tokenizer-aware chunking (owner: Claude; due: 2026-10-10)

## Appendix A — Requirements register

| ID | Title | Gap (short) | Action (short) | Acceptance (short) | Gate | Impl status | Evidence | Owner role |
|---|---|---|---|---|---|---|---|---|
| OPS-011 | Voyage tier | No card. | Add a card. | Tier screenshot. | LAUNCH | implemented (closed 2026-09-22) | production | Founder |
| UX-009 | Desktop app | No GUI. | Tauri. | Installs. | SCALE | planned | unknown | Claude |
| FIN-001 | Mix model | Codex mix. | Superseded. | — | — | rejected (no mix target) | reported/calc | — |
| LEG-003 | Terms billing | Missing clause. | Draft it. | Clause live. | LAUNCH | planned | unknown | Claude |
| SEC-019 | Org helpers | Leaks membership. | Revoke execute. | Advisor clean. | W2 | planned | code-read | Codex |

Another table in the appendix (not register rows):

| ID | Resolution | Note |
|---|---|---|
| RET-001 | rejected | this 3-column row must not count as a register row |

## Appendix B — Evidence and sources

| RET-001 | Not | a | register | row | LATER | rejected | x | y |
