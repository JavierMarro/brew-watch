# Security

brew-watch treats local state and Homebrew's package metadata as untrusted data.
It does not treat a compromised user account, interpreter, Homebrew installation,
tap, or package installer as safe. This is a local tool, not a sandbox for Homebrew.

## Safeguards

- Homebrew execution is limited to `brew outdated --json=v2`. brew-watch never
  invokes `update`, `upgrade`, `install`, `migrate`, or `sudo`.
  Full auto-update is disabled; API metadata refresh is enabled explicitly.
  Homebrew may write its API cache and make network requests. Third-party tap
  definitions are not refreshed; cached API data may be used if refresh fails.
  A valid cached report starts the 14-day interval even when refresh failed.
  Completion timestamps are not proof of metadata freshness.
  Public commands are `status` and `--help`; neither contacts Homebrew.
- Subprocess arguments are lists. Package names and versions are never evaluated,
  used to build shell commands, or passed back as executable arguments.
- Setup generates a launcher with a pinned Python interpreter and isolated mode.
  Homebrew is resolved from the user's PATH to its real executable; unsafe file
  ownership or group/world-writable executable and parent-directory permissions
  are rejected, including intermediate directories in symlink chains. The child
  PATH retains only the validated, resolved executable directory plus system
  directories, preventing later retargeting of the original directory symlinks
  from redirecting helper lookup. Resolved PATH directories containing a colon
  are refused. No `brew` executable or alias is replaced.
- Child environments use a small allowlist, omitting shell, Ruby, Python, dynamic
  loader, and Git executable-injection environment settings. Proxy/certificate
  configuration is retained. Analytics are disabled for these Homebrew invocations
  without editing the user's persistent Homebrew configuration.
- Background commands have no interactive stdin. They run in process groups with
  time and captured-output bounds. The output limit does not cap Homebrew downloads. During checks, interrupt, terminate, and hangup
  signals clean up the group before the
  operation lock is released. Repeated signals do not interrupt cleanup, and
  previous handlers are restored afterward. The shell hook suppresses launcher
  and interpreter stderr and emits a fixed failure notice instead. Raw captured stderr is never printed
  or stored. SIGKILL and system crashes cannot be handled.
- State lives in a user-owned mode-0700 directory, with mode-0600 files. File
  access rejects symlinks, hard links, special files, unsafe ownership, and unsafe
  permissions. Directory traversal beneath the home directory uses directory file
  descriptors and no-follow opens. An atomic replacement and fsync preserve the
  previous state if writing is interrupted.
- Native macOS ACL checks reject write grants on protected paths and additionally
  reject read/search grants on private files and directories. Inherited grants
  are checked, including on staged files before writing private data. Grants are
  conservatively rejected regardless of their principal; deny-only ACLs remain
  allowed. ACL read errors fail closed. Permission validation requires macOS.
- The JSON schema and sizes are checked. Terminal controls and invisible format
  characters are removed from brew-watch's own package/error output, including
  command-line parser errors in the tool and installer. Locks prevent
  simultaneous scans across terminals, and failed checks retain the
  last successful result. Future timestamps caused by a backward clock correction
  remain readable; checks wait for their existing intervals and status reports the discrepancy.
- A new scan replaces the in-memory successful snapshot only after saving it.
  Successful state leaves room for a later error message. Automatic reports are
  limited to 20 package lines of 240 characters each. The zsh callback reads one
  available chunk at a time and caps its buffer, rather than blocking on a line
  or draining an arbitrarily long report while the user types.
- Setup is explicit, repeatable, and reversible. It backs up `.zshrc`, preserves
  unrelated bytes, revalidates contents and file safety immediately before
  replacement, and refuses unrelated files at its installation paths. Detected
  concurrent edits abort setup/removal without replacing the edited file.
  Setup/removal refuse any existing ACL on `.zshrc`, including deny-only entries,
  both when first reading it and immediately before replacement. This preserves
  restrictions that copying only ordinary permission bits would lose.
  Development tests never activate the real hook.

## Trust limits

The user's shell configuration, PATH, Python installation, and selected Homebrew
installation must be trusted. brew-watch is not setuid and rejects root execution.
Processes running as the same user can alter the program or data; mode bits cannot
isolate applications from other processes belonging to that same account. Homebrew
may execute its own configuration, tap logic, downloads, and package installer code.
brew-watch does not validate or sandbox those internals. brew-watch does not directly
request package upgrades or installations.

Close editors of `.zshrc` before setup/removal. The installer lock cannot exclude
an unrelated editor writing between the final check and atomic replacement.
Backups contain the version read at setup start, not every concurrent edit.
Process cleanup covers the captured command's process group; a descendant that
deliberately creates a separate session is outside that boundary.

These safeguards reduce specific risks; they are not a security certification.
See the README for the exact tested and untested boundaries. Report a suspected
issue privately to the maintainer through the repository host's private security
reporting feature if available. Do not include credentials or private package data
in a public issue.
