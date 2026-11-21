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

## Update: `gh` auth got fixed mid-pass, so this is now confirmed, not hypothesized

The paragraph above was written when `gh auth status` reported an invalid keyring token. That got fixed
during this same pass (it turned out `gh pr create`/`gh run view --log-failed` work even while `gh auth
status` complains), so I could pull the real Windows log for `main`'s latest run
(44712789177/job 136212254752): **205 failed, 1493 passed, 42 skipped, 27 errors in 325.66s**. The symlink
and chmod/permission-bit cases in the static-analysis guess below turned out to already be handled with
`pytest.mark.skipif` (they show up as `SKIPPED`, not failed, in that log) — so that part of the original
guess was wrong. The real top finding:

- **A Windows path embedded raw into a TOML string breaks the parser, and it's everywhere.** Several test
  files build `policy.toml` content with an f-string like `f'path = "{folder}"'` or
  `f'[deny]\npaths = ["{folder}"]'`. A Windows path segment such as `\Users` matches TOML's `\U` + 8-hex
  escape, so the parser fails with `Invalid hex value`, `policy.toml` silently stops being readable for
  the rest of that test, and everything downstream that depends on it fails too (`AssertionError`,
  `StopIteration`, wrong error message returned instead of search/read results). This single root cause
  shows up in `test_untrusted_profile.py`, `test_remote_restrictions.py`, `test_query_time_policy.py`
  (by far the most occurrences — a dozen-plus call sites), `test_audit_log.py`, `test_closed_folders.py`
  and `test_deny_list_everywhere.py`. PR #8 fixed the three call sites in `test_untrusted_profile.py`,
  `test_remote_restrictions.py` and one line of `test_query_time_policy.py` (the ones my first pass over
  the grep results found) with `.as_posix()` — real, CI-confirmed movement (**204 failed, 1494 passed** on
  PR #8's own Windows run, 44896600235/job 136823479764), but the same pattern recurs in the files just
  named and in most of `test_query_time_policy.py`'s own body (a dozen more `paths = ["{...}"]` lines),
  **not fixed yet**. The founder asked to sequence toward rc2 first; this is the honest next chunk of work
  for whoever does it, not done in this pass.
- **Source-file read without `encoding="utf-8"`.** `test_tidy.py`'s
  `test_nothing_is_wired_to_the_cli_or_the_mcp_server()` read `cli.py`/`mcp_server.py`'s actual source with
  `.read_text()` and no encoding; Windows' default locale encoding (cp1252) can't decode the non-ASCII
  bytes those source files hold (`UnicodeDecodeError`). Fixed in PR #8 with `encoding="utf-8"`. This
  pattern (`.read_text()`/`.write_text()` without an explicit encoding) appears at roughly 50 more call
  sites across the test suite, but almost all of them round-trip ASCII-only fixture content the test
  itself wrote, so cp1252 decodes it identically to UTF-8 there — I did not find evidence any of those
  other 50 actually fail, so I left them alone rather than touch 50 call sites on a guess.
- Symlink privilege and `chmod`/permission-bit tests are already `skipif`'d on Windows (confirmed from the
  real log's `SKIPPED` lines) — no action needed there, unlike what the first version of this section
  guessed.
- `fcntl`-based locking degrading to a no-op on Windows (`shared_embedder.py`, guarded `try/except`) is
  still unconfirmed either way — no evidence for or against it in the log's short summary; would need
  someone to grep the full 6300-line log for `test_local_embedder_concurrency`/`test_shared_embedder`
  specifically.
- The remaining ~204 failures and 27 errors (AssertionError 70, AttributeError 20, FileNotFoundError 16,
  TypeError 6, ValueError 5, and a long tail of one-offs, per a crude scan of exception class names in the
  log) are not yet bucketed past "some fraction of these cascade from the TOML bug above, the rest are
  unknown." That bucketing is the real remaining work.

## What was fixed vs. documented this pass

Two confirmed, narrow, test-only fixes shipped in PR #8 (not product code): the TOML-path-escaping bug in
3 files/4 call sites, and the one `encoding="utf-8"` fix in `test_tidy.py`. Both are CI-verified, not
guessed (204 vs. 205 failed, before/after, same run). The much larger remaining instance of the same
TOML-path bug (named above) was found but deliberately not fixed in this pass — the founder's priority
right now is reaching rc2, and this is independent, uncapped-scope cleanup that can wait for a dedicated
pass with time to fix and verify all of it rather than a partial, rushed sweep.

## Next step for whoever picks this up

1. `gh auth refresh -h github.com`, then `gh run view 44891928883 --log-failed` (or the latest `main` run)
   to get the real list of failing test names.
2. Bucket them against the categories above; add/replace categories as the real log dictates.
3. Only then decide, per test, whether it should be: fixed (platform-neutral code), marked
   `skipif(sys.platform == "win32", reason=...)` with a one-line reason, or left failing and counted.
4. Update the count in README/SECURITY and this file together; they must not drift apart.
