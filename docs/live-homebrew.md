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
The smoke check calls only `brew outdated --json=v2` with the production restricted
environment: full auto-update disabled, API metadata refresh enabled. Homebrew
may access the network and write its API cache. No shell hook is installed.

The script refuses execution unless the platform and GitHub environment checks
pass. These checks prevent accidents, not deliberate environment-variable
spoofing. On disposable runners only, it tightens runner-owned Homebrew parent
permissions when necessary. It never relaxes production permission checks.

Keep this manual workflow separate from required PR checks: it depends on live
Homebrew and network availability. Actions are pinned, checkout credentials are
not retained, no repository secrets are needed, and each job has a five-minute
limit. The production scan retains its 60-second timeout and output limit.

## What a pass establishes

The selected Homebrew executable passed validation, and its real outdated report
parsed successfully with API refresh enabled. Package names and raw errors are
not published in the summary. A pass does not independently establish metadata
freshness: Homebrew can use cached API data, and third-party tap definitions are
local. It also does not prove every cask or tap is compatible.

Local tests cover report parsing, command/environment selection, failure handling,
process cleanup, security checks, concurrency, installation, and the real zsh hook
with fake Homebrew. Hosted checks are still required for live compatibility.
