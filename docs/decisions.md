# Decisions (ADR log)

Append-only: a new decision is a new entry below, numbered in order. Never edit a prior entry — if a
decision changes, add a new one that says so and references the old number. This file did not exist in
the `locusfiles` repo before ADR-001; the engine's older ADR history (ADR-001 through ADR-01x) lives in
the private `locus` repo's `docs/decisions.md`, not here.

## ADR-001 — Windows CI failure is a known, accepted, non-blocking baseline (2026-11-21)

**Status:** accepted.

**Context:** PR #4 (`fix/secret-names-cookies`, content-based secret/cookie detection), #5
(`chore/site-url-constant`) and #6 (`chore/soak-parallel-search`) all showed `test-windows` failing in CI.
Before merging them further or treating this as a regression, it needed to be established whether these
three PRs caused the failure or whether it predates them.

**Decision:** `test-windows` failing is the accepted, intentional baseline, not a regression from PR
#4/#5/#6 or from anything merged alongside them. Evidence: `.github/workflows/ci.yml` and
`os-matrix.yml` mark every Windows job `continue-on-error: true` with an explicit comment that Windows is
not supported/claimed yet and the job exists only to report, never to block; the wording and the "~200
tests fail on Windows, unsorted" count were added by PR #2 (`fix/windows-evidence-and-flaky-test`, merged
2026-11-19), before PR #4/#5/#6 existed; and `main`'s own latest CI run shows the identical pattern
(`test-windows` job conclusion `failure`, workflow run conclusion `success`). None of PR #4/#5/#6 touch
`.github/workflows/*` or add Windows-specific code. Full evidence and a static-analysis hypothesis for
*why* (symlink privilege, `chmod`/permission-bit assumptions, `fcntl`-based locking degrading to a no-op)
is in `agents/engineering/windows-triage.md`.

**Consequence:** PR #4/#5/#6 were not held back on `test-windows`; the founder in fact merged all three
(2026-11-20T09:40Z, before this ADR was written). Windows continues to be documented as "not verified as
supported" everywhere the README/SECURITY text already says so — this ADR does not change that claim, it
records why the CI signal looks the way it does. The actual categorized breakdown of the ~200 Windows
test failures was **not** confirmed against real CI logs in this pass (`gh` auth was broken — invalid
keyring token for `yonilev2003` — and the anonymous GitHub API refuses job-log downloads even on a public
repo); `windows-triage.md` says what the next pass needs to do once `gh auth refresh -h github.com` has
been run.
