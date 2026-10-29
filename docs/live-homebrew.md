# Live Homebrew check

This manual workflow runs the production outdated-package scan on disposable
GitHub-hosted macOS runners, on Intel and Apple Silicon. It replaces the former
migration-recovery experiment: brew-watch no longer invokes `brew update`.

## Run it

1. Merge these changes through a PR after regular CI passes.
2. Open **Actions → Live Homebrew check → Run workflow**.
3. Select `main` and start a new run. Rerunning an old recovery run uses its old code.
4. Read each job's **Real Homebrew outdated report** step and run summary.

Two jobs run, one per architecture. No fixture package is installed, no tap is
created or trusted, and no deliberate migration or interruption is triggered.
The harness records the commit, macOS, Python, and Homebrew versions (`brew --version`),
then runs `brew outdated --json=v2` with the production restricted environment.
Full auto-update stays disabled and API metadata refresh stays enabled. A test-only
`HOMEBREW_CACHE` override isolates all API-cache experiments in a temporary directory.
No shell hook is installed.

The script refuses execution unless the platform and GitHub environment checks
pass. These checks prevent accidents, not deliberate environment-variable
spoofing. On disposable runners only, it tightens runner-owned Homebrew parent
permissions when necessary. It never relaxes production permission checks.

Keep this manual workflow separate from required PR checks: it depends on live
Homebrew and network availability. Actions are pinned, checkout credentials are
not retained, no repository secrets are needed, and each job has a ten-minute
limit. The production scan retains its 60-second timeout and output limit.

## What a pass establishes

Every phase must pass:

1. **Empty cache:** a real outdated report parses and populated signed API catalog
   files appear in the temporary cache.
2. **Stale cache:** those catalogs are dated 30 days in the past. Another scan must
   return a valid report and advance their modification times. Timestamp movement
   alone does not prove a successful download or conditional revalidation.
3. **Rejected refresh with cache:** catalogs are dated again. A temporary loopback
   proxy returns HTTP 502 for every request. The scan must still produce a valid
   report, proxy requests must be observed, and the catalog file set, sizes, and
   SHA-256 hashes must remain unchanged. Timestamp-only changes are recorded but
   allowed: Homebrew can touch cached files during failed refreshes. This
   demonstrates cached fallback, not fresh metadata.
4. **Rejected refresh without cache:** a separate empty cache must fail, and the
   proxy must observe requests. This guards against a test that never attempted
   to refresh metadata.

The proxy exists only during the test on the disposable runner, binds only to
127.0.0.1, never forwards requests, and never logs URLs, headers, or credentials.
Production brew-watch has no proxy or network listener. Proxy environment overrides
are test-only; this is not a system-wide network block.

The test fails if Homebrew changes its signed-catalog layout or fallback behavior;
it must not silently skip a phase. Each command retains its production time and
output limits. Raw command errors and package names are not published in summaries.

If fallback verification fails after a valid report, the summary records rejected
request counts and counts of added, removed, content-changed, and timestamp-changed
catalogs. The empty-cache control still runs to distinguish a proxy problem from
a content change. Missing proxy requests or changed catalog files/contents still
fail verification. No catalog paths or contents are logged.

A pass verifies these cache behaviors on the recorded Homebrew version. It is not
a guarantee that every later report is fresh: production accepts valid cached
reports, which also start the 14-day interval. Third-party tap definitions remain
local, and this does not establish compatibility with every cask or tap.

Local tests cover report parsing, command/environment selection, failure handling,
process cleanup, security checks, concurrency, installation, and the real zsh hook
with fake Homebrew. Hosted checks are still required for live compatibility.

## Release acceptance

Use one candidate commit for all four required CI checks and both live jobs.
Record their run links and the version information from each live summary. Confirm
branch protection still requires the existing check names. Then explicitly test
installation, multiple terminal tabs, partially typed input, status, and removal
in an isolated home; compare unrelated shell configuration before and after.
Perform a real-shell acceptance check only with the user's permission. Keep the
README marked pre-release until live verification and acceptance are complete.
