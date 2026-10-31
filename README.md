# brew-watch

![brew-watch — Keep your brews from going stale.](assets/brew-watch-banner.svg)

**Keep an eye on your Homebrew. Keep your brews fresh.**

[macOS 15+](#requirements) · Python 3.11+ · zsh · [MIT licensed](LICENSE)

Two years after installing Homebrew while learning to code, I discovered I was
supposed to maintain its formulae and casks. It only took six hours to update almost 30
of them.
brew-watch was created as the reminder I wish I’d had.

Open an interactive zsh terminal. If 14 days have passed since the last completed
report, brew-watch checks installed formulae and casks with `brew outdated`.
Cycling dots show when a check is running. If updates are available, a short
reminder appears while preserving whatever you were typing. Use `brew-watch status`
for the last result, next check date, and metadata details.

brew-watch never requests package upgrades or installations. You decide when to
maintain your packages through Homebrew. Automatic checks allow API metadata
refresh without running `brew update`; cached metadata may still be used.

No menu-bar app, launchd schedule, telemetry, account, cloud service, or third-party
Python packages. Oh My Zsh is optional.

brew-watch is a separate program: it does not patch zsh or Homebrew. A small,
marked block in your `.zshrc` asks zsh to start it when a terminal opens. It calls
Homebrew's existing commands, just as you would at the prompt. Removing that
block disables automatic checks; the installed launcher can still be run manually.

> **Pre-release.** Review the source and
> [verification limits](#development-and-verification) before installing.

## Requirements

- Initial target: macOS 15+ on Intel or Apple Silicon, using interactive zsh with
  its standard line editor (ZLE).
- Python 3.11+ and an existing Homebrew installation available on `PATH`.
- Load Homebrew's normal shell setup before brew-watch's startup block.

Homebrew has its own platform policy. Intel is currently Tier 3; Homebrew plans
to stop running on Intel in or after September 2027. brew-watch cannot extend that
support. See [Homebrew's support tiers](https://docs.brew.sh/Support-Tiers).

## Local installation

Review the source, then run this from the repository directory:

```sh
python3 -I install.py
```

Open a **new terminal**. The first check runs automatically, without blocking the
prompt. The installer never runs Homebrew and does not require sudo.

While a due check runs, zsh shows “Automatically checking your formulae and casks”
with dots cycling from one to three. You can keep typing. The indicator clears
when the check finishes or fails; if nothing is outdated, no reminder follows.
Terminals opened before the next check is due stay quiet. The indicator updates
while zsh's line editor is active, not over the output of a command you are running.

Setup copies the program into `~/.local/share/brew-watch`, creates a launcher at
`~/.local/bin/brew-watch`, and appends a clearly marked block to `.zshrc` (under
`ZDOTDIR` if set). The block defines the `brew-watch` command in interactive zsh,
so changing your PATH is unnecessary. Setup refuses a resulting `.zshrc` larger
than its 1 MiB read limit so later removal remains possible. Existing startup content is preserved and
backed up before any edit. Re-running setup updates this installation without
duplicating the block.

Save and close editors of `.zshrc` before setup or removal. Setup checks the file
again immediately before replacement and aborts if its contents changed, leaving
the edit in place. Its lock coordinates installers, not unrelated editors; an
edit racing the final check and replacement cannot be excluded. Keep the printed
backup until you have reviewed the result.

The launcher pins the Python executable used during setup and enables Python's
isolated mode. If that interpreter is removed, rerun setup using a working Python.
Installing from a different checkout updates the same local installation.

Setup deliberately refuses symlinked `.zshrc` files, group/world-writable home
paths, unrelated files at its destinations, and unsafe ownership. If you manage
dotfiles through symlinks, inspect and adapt the shell integration manually rather
than weakening those checks. `ZDOTDIR` must be an absolute path inside your home.

Permission checks include macOS ACLs, including inherited grants. Write grants
are refused on protected paths; private state and newly written private files
also refuse read/search grants. This is conservative: even grants to the owner
are refused, while deny-only entries are allowed on other protected paths.
Setup and removal refuse any existing ACL on `.zshrc`, including deny-only ACLs,
so replacing the file cannot silently remove access restrictions. These files
require manual shell integration/removal that preserves their ACLs. Setup never
changes existing permissions or ACLs to make a check pass.

## Commands

```sh
brew-watch status
brew-watch --help
```

- **status** reads local history without contacting Homebrew: last completed
  report, last-known outdated count, and the next check due. A due check runs only
  when a new interactive terminal opens. Status also shows the last check error.
- **--help** explains the commands and the automatic check.

Outside interactive zsh, the installed launcher is at `~/.local/bin/brew-watch`.
The private `_startup` entry point is for the hook, not an additional public command.

The automatic check runs only `brew outdated --json=v2`, with
`HOMEBREW_NO_AUTO_UPDATE=1` and `HOMEBREW_FORCE_API_AUTO_UPDATE=1`.
This allows Homebrew to refresh API metadata for official formulae and casks
without running the full `brew update`. It does not update Homebrew itself,
refresh third-party Git taps, or request package migrations, installations, or upgrades.

**Freshness limits:** third-party taps are checked against their local definitions;
refresh those yourself with Homebrew when needed. API refresh follows Homebrew's
cache policy, and Homebrew may fall back to cached data on network errors. A
successful check means Homebrew returned a valid report, not that every definition
was freshly downloaded. A valid cached report also starts the 14-day interval,
so a failed refresh can delay discovery until the next due check even after
connectivity returns. `status` reports completion, not verified freshness.
No separate version lookup service is used.

Automatic failures show a fixed message directing you to `status`; raw launcher and
interpreter diagnostics are suppressed by the hook. Automatic failures are retried
only on a new terminal opening, after a one-hour cooldown. Multiple terminals share a lock and the last completed report timestamp.
Automatic reminders show at most 20 packages, with each package line limited to
240 characters. Run `brew outdated --verbose` for the full list. The shell reads
available output in small chunks without waiting for a complete line, and displays
the bounded reminder when the worker closes its output.

Illustrative output, not a scan of your machine:

```text
🍺 These brews could use a little attention:

Preview of 2 packages: up to 20 entries; long lines shortened.

  Formula: example-tool  1.0 -> 2.0
  Cask: example-app  3.0 -> 4.0

Inspect them with Homebrew: brew update && brew outdated --verbose
```

The suggested command is for you to run manually: `brew update` updates Homebrew
and package definitions, then `brew outdated --verbose` lists outdated packages
only if the update succeeds. It does not upgrade installed packages. The automatic
check never runs this command.

Pinned formulae are labelled and left to Homebrew's upgrade rules. Homebrew normally
excludes self-updating casks and casks with an unspecified latest version; brew-watch
does not enable greedy mode. brew-watch does not directly request package upgrades or installations.

## Removal

From this checkout:

```sh
python3 -I install.py --uninstall
```

Open a new terminal afterward. Removal deletes only managed program files and the
marked startup block; unrelated shell settings and backups remain. A private
installer lock and private scan history under `~/.local/state/brew-watch` are
retained. Removing the hook does not cancel a check already running in an old
terminal. No Homebrew packages are removed or changed by setup/removal.

## Security and limitations

See [SECURITY.md](SECURITY.md) for the trust boundaries and concrete safeguards.
No script can guarantee "maximum security" independently of the software it runs.

- The background package check has a 60-second timeout. Captured command output
  and stored state are size-limited; these limits do not
  cap network downloads. Homebrew manages its own downloads and cache.
- Failure messages omit raw Homebrew output to avoid exposing secrets or terminal
  escapes. For diagnosis, run `brew outdated --verbose` yourself.
- Interrupted captured checks terminate their process group before releasing the
  operation lock. An uncatchable kill (`SIGKILL`) or system crash cannot run cleanup.
- State is private JSON, never executable code. Corrupt or unsafe state is refused
  without overwriting it. Inspect `~/.local/state/brew-watch/state.json`; to reset
  history, close other terminals/checks, preserve a copy if needed, and remove only
  that file. Do not remove a live lock file. A backward clock correction does not
  invalidate history: status reports future timestamps, and automatic checks wait
  until the saved check/retry interval has elapsed. Correct the system clock if needed.
- A terminal closed before its check finishes may miss that reminder. The saved count, timestamps, and error
  remain available through `status` (not the saved package list); the next automatic scan is due 14 days after
  the last completed report. The tool does not persistently nag about old results.
- brew-watch deliberately passes a restricted environment to Homebrew. Custom
  executable-injection variables and `HOMEBREW_*` environment overrides are not
  inherited. Proxy and certificate settings are retained. Homebrew's own stored
  configuration still applies. Use Homebrew directly for workflows requiring
  other environment overrides.
- Homebrew's executable and every directory traversed while resolving it,
  including intermediate symlink locations, must have safe ownership and no
  group/world write permissions or ACL write grants. Some shared or admin-group
  writable Homebrew installations will therefore be refused. Review those
  permissions separately; brew-watch does not repair them automatically.
  The child `PATH` uses the validated, resolved executable directory so retargeting
  an original directory symlink cannot redirect helper commands. Resolved directory
  names containing `:` are refused because that character separates `PATH` entries.
- Saved reports contain only the package count, timestamps, and any check error;
  package names and versions are used only for the immediate reminder. Older
  reports remain readable through `status` and are reduced to counts atomically
  under the lock on the next terminal opening, without changing the check schedule.
  Nothing is uploaded by brew-watch, but Homebrew makes its usual network requests.

## Development and verification

```sh
python3 -I -m unittest discover -s tests -v
python3 -I -m py_compile brew_watch.py install.py scripts/*.py tests/*.py
/bin/zsh -n shell/brew-watch.zsh
git diff --check
```

Tests use isolated temporary homes and a fake Homebrew executable, including a real
interactive zsh session through a pseudo-terminal. They never upgrade host packages
or edit your real `.zshrc`. Live Homebrew API refresh/reporting and Apple Silicon hardware
compatibility require separate verification; passing mocked tests does not establish
either. The first implementation is a local install, not a published Homebrew formula.

The GitHub Actions workflow runs these checks on macOS 15 Intel and ARM runners
with Python 3.11 and 3.15. Actions are pinned to full commit IDs, the token has
read-only repository access, and checkout does not retain credentials. CI uses
fake Homebrew too; a passing workflow is not a live Homebrew compatibility test.
Repository administrators must separately require these checks in branch rules
if they want merges blocked on failures.

For repeatable terminal timings, run `python3 -I scripts/benchmark_startup.py`.
It compares the hook enabled/disabled with fresh compact state and near-limit
legacy state (10 batches per case), plus due scans returning a large Unicode
report (3 batches per case). Each case opens one or four terminals at once.
Active cases repeatedly send input during progress and reminder delivery, require
both to appear, and continue for three responses per terminal after the reminder.
The benchmark checks that concurrent terminals run exactly one scan per batch.
It reports median and maximum prompt/input latency in milliseconds and runs in CI
with a generous 1,000 ms per-sample regression limit. That limit catches stalls;
it is not a target or guarantee of acceptable interactive latency. All cases use
temporary homes and fake Homebrew. Run separately from other test workloads.

For the separate, manually triggered live reporting workflow, see
[Live Homebrew check](docs/live-homebrew.md). It runs on disposable GitHub-hosted
macOS runners without installing a fixture or triggering a migration.

## License

[MIT License](LICENSE). Copyright (c) 2026 Javier Martínez Romera and
brew-watch contributors.
