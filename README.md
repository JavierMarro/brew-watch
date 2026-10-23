# brew-watch

![brew-watch — Keep your brews from going stale.](assets/brew-watch-banner.svg)

**Keep an eye on your Homebrew. Keep your brews fresh.**

[macOS 15+](#requirements) · Python 3.11+ · zsh · [MIT licensed](LICENSE)

Two years after installing Homebrew while learning to code, I discovered I was
supposed to maintain its formulae and casks—and spent two hours updating 20-odd
of them. brew-watch is the reminder I wish I’d had.

Open an interactive zsh terminal. If 14 days have passed since the last successful
check, brew-watch refreshes Homebrew and checks installed formulae and casks.
It reports updates while preserving whatever you were typing. Nothing is upgraded
until you explicitly run `brew-watch freshen`.

No menu-bar app, launchd schedule, telemetry, account, cloud service, or third-party
Python packages. Oh My Zsh is optional.

> **Pre-release.** Further audits and live Homebrew testing are pending. Review
> the source and [verification limits](#development-and-verification) before installing.

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

Setup copies the program into `~/.local/share/brew-watch`, creates a launcher at
`~/.local/bin/brew-watch`, and appends a clearly marked block to `.zshrc` (under
`ZDOTDIR` if set). The block defines the `brew-watch` command in interactive zsh,
so changing your PATH is unnecessary. Existing startup content is preserved and
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
are refused, while deny-only entries are allowed. Setup never changes existing
permissions or ACLs to make a check pass.

## Commands

```sh
brew-watch status
brew-watch freshen
brew-watch --help
```

- **status** reads local history without contacting Homebrew: last successful
  check, last-known outdated count, and the next check due. A due check runs only
  when a new interactive terminal opens. Status also shows the last check error.
- **freshen** explicitly refreshes Homebrew, previews outdated packages, and runs
  `brew upgrade --ask`, retaining Homebrew's confirmation and normal privilege
  handling. It requires an interactive terminal. A successful upgrade is followed
  by another scan so status reflects the remaining updates.
- **--help** explains the commands and the automatic check.

Outside interactive zsh, the installed launcher is at `~/.local/bin/brew-watch`.
The private `_startup` entry point is for the hook, not an additional public command.

The automatic check uses `brew update` followed by `brew outdated --json=v2`.
**`brew update` changes Homebrew itself and its package information**, but does not
upgrade your installed packages. A failed refresh never proceeds to an upgrade.
Automatic failures are retried only on a new terminal opening, after a one-hour
cooldown. Multiple terminals share a lock and the last successful check timestamp.
Automatic reminders show at most 20 packages, with each package line limited to
240 characters. Run `brew outdated --verbose` for the full list. The shell reads
available output in small chunks without waiting for a complete line, and displays
the bounded reminder when the worker closes its output.

Illustrative output, not a scan of your machine:

```text
These brews could use a little attention:

  Formula: example-tool  1.0 -> 2.0
  Cask: example-app  3.0 -> 4.0

Freshen eligible packages: brew-watch freshen
Or inspect them: brew outdated --verbose
No packages have been upgraded.
```

Pinned formulae are labelled and left to Homebrew's upgrade rules. Homebrew normally
excludes self-updating casks and casks with an unspecified latest version; brew-watch
does not enable greedy mode. Updating one package can affect dependencies, and an
upgrade is not a database migration. Follow the package's own upgrade instructions.

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

- The background refresh has a 120-second timeout; the package scan has a
  60-second timeout. Output and stored state are size-limited. Manual upgrades
  are not forcibly timed out, since stopping a package installer can leave it
  partially applied; use Homebrew's normal interaction to stop one.
- Failure messages omit raw Homebrew output to avoid exposing secrets or terminal
  escapes. For diagnosis, run `brew update` or `brew outdated --verbose` yourself.
- Interrupted captured checks terminate their process group before releasing the
  operation lock, including during `freshen`. An uncatchable kill (`SIGKILL`) or
  system crash cannot run cleanup. If post-upgrade verification fails, status
  warns that packages may have changed; its package count remains last-known data.
- During the interactive upgrade, brew-watch defers its own interrupt, terminate,
  and hangup handling until Homebrew exits, retaining the operation lock. Terminal
  signals such as Ctrl-C still reach Homebrew normally. A signal sent only to
  brew-watch does not forcibly stop an installer. Status records an unverified
  upgrade before it starts, so an interruption cannot leave an apparently clean
  result. SIGKILL and system crashes can still release the lock early.
- State is private JSON, never executable code. Corrupt or unsafe state is refused
  without overwriting it. Inspect `~/.local/state/brew-watch/state.json`; to reset
  history, close other terminals/checks, preserve a copy if needed, and remove only
  that file. Do not remove a live lock file.
- A terminal closed before its check finishes may miss that reminder. Results
  remain available through `status`; the next automatic scan is due 14 days after
  the last successful scan. The tool does not persistently nag about old results.
- brew-watch deliberately passes a restricted environment to Homebrew. Custom
  executable-injection variables and `HOMEBREW_*` environment overrides are not
  inherited. Proxy and certificate settings are retained. Homebrew's own stored
  configuration still applies. Use Homebrew directly for workflows requiring
  other environment overrides.
- Homebrew's executable, its resolved parent directories, and the original
  executable directory retained in `PATH` must have safe ownership and no
  group/world write permissions or ACL write grants. Some shared or admin-group
  writable Homebrew installations will therefore be refused. Review those
  permissions separately; brew-watch does not repair them automatically.
- Cached package names are local data. Nothing is uploaded by brew-watch, but
  Homebrew's commands make their usual network requests.

## Development and verification

```sh
python3 -I -m unittest discover -s tests -v
python3 -I -m py_compile brew_watch.py install.py tests/test_brew_watch.py
/bin/zsh -n shell/brew-watch.zsh
git diff --check
```

Tests use isolated temporary homes and a fake Homebrew executable, including a real
interactive zsh session through a pseudo-terminal. They never upgrade host packages
or edit your real `.zshrc`. Live Homebrew refresh/upgrade and Apple Silicon hardware
compatibility require separate verification; passing mocked tests does not establish
either. The first implementation is a local install, not a published Homebrew formula.

The GitHub Actions workflow runs these checks on macOS 15 Intel and ARM runners
with Python 3.11 and 3.15. Actions are pinned to full commit IDs, the token has
read-only repository access, and checkout does not retain credentials. CI uses
fake Homebrew too; a passing workflow is not a live Homebrew compatibility test.
Repository administrators must separately require these checks in branch rules
if they want merges blocked on failures.

## License

[MIT License](LICENSE). Copyright (c) 2026 Javier Martínez Romera and
brew-watch contributors.
