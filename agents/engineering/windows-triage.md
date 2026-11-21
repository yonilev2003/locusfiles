# Windows CI triage

Written 2026-11-21, during the rc2 prep pass over PR #4 (`fix/secret-names-cookies`), #5
(`chore/site-url-constant`) and #6 (`chore/soak-parallel-search`). All three were already merged by the
founder (closed as merged 2026-11-20T09:40Z, before this pass started) and are the tip of `main` (merge
commit `300bd0f`, via `a183d13` -> `57b1fe9` -> `300bd0f`). This note is about `test-windows`, which fails on
all three PRs and on `main` itself.

## Verdict: pre-existing, by-design baseline — not caused or worsened by PR #4/#5/#6

Evidence, not inference:

1. `.github/workflows/ci.yml` line 38: `# Windows is not supported yet (README says so): this job only
   reports what fails, and never blocks a merge.` The `test-windows` job (line 39-41) has
   `continue-on-error: true`. `.github/workflows/os-matrix.yml` line 6 says the same thing for its own
   Windows leg.
2. On `main`'s latest CI run (run id `44712789177`, commit `300bd0f`, 2026-11-20T09:41Z) the `test-windows`
   job's own conclusion is `failure`, but the **workflow run's** conclusion is `success` — exactly what
   `continue-on-error: true` produces. CI is not red; it was never supposed to be.
3. The `continue-on-error`/"Windows not supported" wording predates PR #4/#5/#6: it was added by PR #2
   (`fix/windows-evidence-and-flaky-test`, commit `8099965`, merged 2026-11-19 as merge commit `0175e7b`),
   two merges before PR #4. Its own commit message already states the baseline: "About 200 of the
   repository's own tests fail on Windows and have not been sorted." The same number is in the public
   README ("about 200 of the repository's own tests fail on Windows and have not been sorted yet, so
   Windows is not verified as supported").
4. PR #4/#5/#6 do not touch `.github/workflows/*`, do not add Windows-only code paths, and their own CI
   runs show the same pattern as `main`: `test`, `gitleaks`, `semgrep`, build/install-smoke all green;
   only `test-windows` red, non-blocking. Nothing in their diffs is Windows-specific.

**Conclusion: this is the documented, intentional pre-existing baseline, not a regression from these three
PRs.** No PR should be held back for it; none were.

## What I could and could not verify today

I could not pull the actual list of ~200 failing Windows test names or their tracebacks: `gh auth status`
reports the keyring token for `yonilev2003` is invalid (`gh auth refresh -h github.com` needed), and the
anonymous GitHub REST API returns 403 on `actions/jobs/{id}/logs` (log downloads need an authenticated
token even on a public repo). I did not attempt to work around that with a different credential, so the
breakdown below is a **static-analysis hypothesis**, grounded in code, not a confirmed count per category.
Re-run `gh run view <run-id> --log-failed` once `gh auth refresh -h github.com` is done, and replace this
section with the real breakdown.

## Likely categories (static analysis of `tests/`, unconfirmed against real failures)

- **Symlink privilege (most likely the largest category).** `os.symlink` / `Path.symlink_to` appears in
  about 10 test files (`test_cli_json.py`, `test_remote_http.py`, `test_closed_folder_routes.py`,
  `test_service.py`, `test_query_time_policy.py`, `test_stat_scan.py`, `test_estimator.py`,
  `test_prune_night_fixes.py`, `test_clients.py`, `test_closed_folders.py`, and others). Creating a
  symlink on Windows needs `SeCreateSymbolicLinkPrivilege` (admin, or Developer Mode) — GitHub's
  `windows-latest` runner does not grant it to the default run-as user, so every such call likely raises
  `OSError: [WinError 1314]` at setup time, failing the whole test (and, where it's in a fixture, every
  test that fixture feeds).
- **Permission bits (`os.chmod`, `0o000`/`0o400`-style modes).** About 10 files assert on
  `PermissionError` or restricted access after `chmod` (`test_remote_http.py`, `test_kill_switch.py`,
  `test_audit_log.py`, `test_clients.py`, `test_lexical.py`, `test_stat_scan.py`, `test_cli_setup.py`,
  `test_remote_guard.py`, `test_prune_night_fixes.py`, `test_api.py`). Windows ACLs don't honour POSIX
  mode bits the same way `chmod` implies on POSIX, so a test expecting a read to fail after `chmod(0o000)`
  can see it succeed instead, or fail for a different reason (`store.py`'s own `0700`-on-POSIX-only logic,
  noted in the README, is the production-code side of the same gap).
- **File locking.** `src/locus/ingest/shared_embedder.py` imports `fcntl` inside a guarded
  `try/except` and sets `fcntl = None` when it's absent (true on Windows); the lock call itself is skipped
  when `fcntl is None` (see line ~423). That's safe — it degrades to "no actual lock" rather than
  crashing — but it means any test asserting mutual exclusion between concurrent embedder/index runs
  (`test_local_embedder_concurrency.py`, `test_shared_embedder.py`) is exercising a different code path
  on Windows and could legitimately see different timing/ordering there.
- Not investigated: path-separator assumptions, console encoding (the journey script was already fixed
  for cp1252 in commit `c4364d1`, before PR #1), and anything specific to `chromadb`'s own Windows support.

## What was fixed vs. documented this pass

Nothing was changed in test or product code for Windows this pass. I judged there was no fix I could
make and verify: I have no Windows machine or Windows CI run available to me, so any change to the
symlink/chmod-dependent tests (e.g. `pytest.mark.skipif` on Windows without the privilege) would be
unverified when "done" — exactly the kind of risk CLAUDE.md's "surgical changes" rule warns against.
Documenting here, plus the ADR below, is this pass's output; the real fix (sorting the ~200 failures into
skip/xfail/fix categories with actual Windows log output) needs either a working `gh` auth or a Windows
runner to iterate against.

## Next step for whoever picks this up

1. `gh auth refresh -h github.com`, then `gh run view 44891928883 --log-failed` (or the latest `main` run)
   to get the real list of failing test names.
2. Bucket them against the categories above; add/replace categories as the real log dictates.
3. Only then decide, per test, whether it should be: fixed (platform-neutral code), marked
   `skipif(sys.platform == "win32", reason=...)` with a one-line reason, or left failing and counted.
4. Update the count in README/SECURITY and this file together; they must not drift apart.
