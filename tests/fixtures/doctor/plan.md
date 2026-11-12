# Fixture plan for tests/test_doctor.py

Template lines like these must be ignored (no real register ID):

`> **[BLOCKER:<GATE>] <ID>** — <description> (owner: …; due: …)`

```bash
grep -n "\[BLOCKER:LAUNCH\]" docs/master-plan/locus-master-plan.md
```

## Ch. A

> **[BLOCKER:LAUNCH] OPS-009** — Buy the domain and point DNS at Vercel (owner: Founder; due: 2026-09-24)

> **[BLOCKER:LAUNCH] OPS-010** — Paid infrastructure tiers verified (owner: Founder; due: 2026-09-20)

> **[BLOCKER:W2] SEC-005** — Security headers reviewed (owner: Claude; due: 2026-10-15)

## Ch. B

> **[BLOCKER:LAUNCH] OPS-009** — Domain purchased and fully migrated (owner: Founder buys, Claude migrates; due: 2026-09-27)

> **[BLOCKER:LAUNCH] SEC-005** — Security headers reviewed (owner: Claude; due: 2026-10-15)

> **[BLOCKER:LAUNCH] OPS-001** — Release-gate table adopted; first manifest written (owner: Claude; due: 2026-09-30)

> **[BLOCKER:LAUNCH] GTM-002** — Remove the unmeasured calculator (owner: Claude; due: 2026-09-21) — CLOSED 2026-09-22, evidence: docs/ops/releases/2026-09-22-abc1234.md

> **[BLOCKER:LATER] CON-006** — Tunnel documented as an alternative (owner: Claude; due: LATER)

  > **[BLOCKER:LAUNCH] SEC-015a** — Indented blocker inside a list (owner: Founder; due: 2026-09-25)
