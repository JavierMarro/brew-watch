# brew-watch

## Product boundary

- A terminal companion to Homebrew for macOS, initially targeting macOS 15+ on Intel and Apple Silicon. Homebrew's own platform support is separate.
- On interactive zsh startup, check only when 14 days have elapsed since the last successful check. No launchd job, persistent daemon, menu-bar app, desktop notifications, cloud service, or telemetry.
- Public interface: `brew-watch status` and `brew-watch --help`.
- Automatic checks may run `brew update` and `brew outdated`. Never run `brew upgrade` or install packages. `brew update` updates Homebrew itself and its metadata.
- Cover formulae and casks. Keep Homebrew's default exclusions for pinned and self-updating packages; do not enable greedy or force modes.
- Use brewing language sparingly in messages; help must explain exactly what commands change.

## Security requirements

- Never invoke a shell to interpret subprocess arguments, use eval, source state files, interpolate package metadata into commands, download executable setup code, or invoke sudo directly.
- Resolve the existing Homebrew executable; do not replace or shadow `brew`. Reject unsafe executable permissions. Never bypass Homebrew's confirmation or privilege checks.
- Never run as root. Automatic processes must have no interactive stdin and must never upgrade packages.
- Keep state private, validate its schema and size, reject symlinks and unsafe ownership/permissions, lock concurrent operations, and replace state atomically.
- Treat Homebrew output and stored data as untrusted. Prevent terminal control sequences in brew-watch's own output. Do not log environment variables, credentials, or raw command errors.
- Bound background command duration and output. Clean up child processes on failure or timeout. A failed check must not count as a successful check; throttle retries.
- Keep terminal startup responsive. Deliver a result without interrupting typed input; avoid duplicate checks across tabs and duplicate hook installation.
- Installation and removal must touch only this tool's files and a clearly marked shell startup block. Preserve unrelated startup content, refuse unsafe files, and retain a backup before editing.
- A user's explicit request to build is not permission to activate the hook in their real shell or upgrade host packages. Use temporary homes and fake Homebrew commands for tests.
- Document trust limits: this tool cannot protect against a compromised user account, Python installation, Homebrew executable, tap, or package installer.

## Engineering and verification

- Read connected callers, tests, setup, and documentation before changes. Finish all affected paths.
- Prefer Python 3.11+ standard library and the system zsh. No new dependencies without a demonstrated need.
- Keep modules and commands minimal. Do not add advisory databases, configuration frameworks, shell support, or packaging services for hypothetical future use.
- Run `python3 -I -m unittest discover -s tests -v`, Python compilation, `/bin/zsh -n shell/brew-watch.zsh`, and `git diff --check`.
- Exercise parsing, command order, failure/timeout, concurrency, permission and symlink attacks, state corruption, terminal escapes, rejection of upgrade commands, and reversible installation. Test the actual zsh hook with a PTY.
- Clearly distinguish mocked verification from real macOS/Homebrew and cross-architecture testing. Do not claim a security guarantee or compatibility that has not been tested.
- Do not create commits, push, or publish without a user request. Do not delegate unless the user explicitly requests it.
