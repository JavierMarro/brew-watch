# Live Homebrew recovery check

This manual workflow uses disposable **GitHub-hosted macOS** runners. It changes
Homebrew on those runners, installs a tiny local test formula, and deliberately
interrupts an update. Never run it on your own Mac or a self-hosted runner. The
script refuses execution unless the platform and GitHub environment checks pass;
these checks prevent mistakes, not deliberate environment-variable spoofing.

## Run it

1. Commit these files on a branch, open a PR, and merge after the regular CI passes.
   GitHub must have `.github/workflows/live-homebrew.yml` on the default branch
   before the **Run workflow** button appears.
2. Open the repository's **Actions** tab.
3. Select **Live Homebrew recovery** in the left sidebar.
4. Click **Run workflow**, select `main`, and confirm **Run workflow**.
5. Open the run. There are four jobs: Intel/ARM, each with signal/timeout scenarios.
   The timeout scenario deliberately waits for brew-watch's 120-second deadline.
6. Read the run summary and each job's **Real Homebrew migration and interruption
   recovery** log. Share failed step output without credentials or private data.

Keep this workflow separate from the four required PR checks. It depends on live
Homebrew and network availability, and intentionally changes runner packages.
No repository secrets are needed; checkout credentials are not retained and the
workflow token has read-only contents access. Each job has a 20-minute limit.

## What counts as a pass

- Real `brew update` and `brew outdated --json=v2` succeed.
- A local tap introduces a formula rename through `formula_renames.json`.
- Homebrew's real migrator moves the installed fixture into its new Cellar path.
- A test-only Ruby checkpoint pauses **after that move**, while migration is still
  incomplete. It does not replace the migration implementation or edit Homebrew's
  source. Missing checkpoints fail the test, rather than silently skipping it.
- brew-watch handles either SIGTERM or its actual 120-second timeout, terminates
  the captured Homebrew process group, releases its lock, and preserves history.
- After removing the pause and advancing only the test state's retry eligibility,
  an automatic retry succeeds, with the expected fixture payload and renamed
  installation receipt. If only an explicit `brew migrate` repairs it, the job
  remains **failed** and reports that manual recovery was necessary.
  The repair check runs even when the automatic retry exits with an error. Logs
  report presence of the old/new fixture payloads. Successful manual repair must
  restore both the payload and receipt and pass another update/outdated scan.

The harness tightens runner-owned Homebrew parent-directory permissions when
needed to satisfy brew-watch's existing checks. Fixture setup confirms Homebrew's
normal installation prompt through a PTY; automatic checks still have no stdin.
Setup explicitly trusts the renamed test formula as well: trust granted to the
old formula by installation does not cover the new name. This is limited to the
fixture on the disposable runner; production brew-watch never grants trust.
If the checkpoint is missed, logs include the worker exit code, the sanitized
saved brew-watch error, and whether successful-check history advanced.
The runner is discarded after the job, including fixture files and trust changes.

## Limits

A green run covers this instrumented formula-rename checkpoint on the recorded
Homebrew revision. It does not establish recovery for every migration phase,
cask installer, pkgconf rebuild, SIGKILL, power failure, or compromised tap.
An initial setup/network failure is inconclusive about migration recovery.
Local unit tests validate the runner guard, PTY confirmation, and recovery-test
control flow; they do not run this destructive integration test. Hosted runs are
required to establish actual recovery behavior.
