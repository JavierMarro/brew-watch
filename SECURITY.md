# Security

brew-watch treats local state and Homebrew's package metadata as untrusted data.
It does not treat a compromised user account, interpreter, Homebrew installation,
tap, or package installer as safe. This is a local tool, not a sandbox for Homebrew.

## Safeguards

- Automatic execution has a fixed command allowlist: `brew update` and
  `brew outdated --json=v2`. It never invokes `upgrade`, `install`, or `sudo`.
- Explicit `freshen` requires terminal input/output. It invokes Homebrew's
  `upgrade --ask`, without force, greedy, automatic yes, or password collection.
- Subprocess arguments are lists. Package names and versions are never evaluated,
  used to build shell commands, or passed back as executable arguments.
- Setup generates a launcher with a pinned Python interpreter and isolated mode.
  Homebrew is resolved from the user's PATH to its real executable; unsafe file
  ownership or group/world-writable executable permissions are rejected. No `brew`
  executable or alias is replaced.
- Child environments use a small allowlist, omitting shell, Ruby, Python, dynamic
  loader, and Git executable-injection environment settings. Proxy/certificate
  configuration is retained. Analytics are disabled for these Homebrew invocations
  without editing the user's persistent Homebrew configuration.
- Background commands have no interactive stdin. They run in process groups with
  time and output bounds. During captured checks in both automatic and manual
  flows, interrupt, terminate, and hangup signals clean up the group before the
  operation lock is released. Repeated signals do not interrupt cleanup, and
  previous handlers are restored afterward. Raw captured stderr is never printed
  or stored. SIGKILL and system crashes cannot be handled.
- State lives in a user-owned mode-0700 directory, with mode-0600 files. File
  access rejects symlinks, hard links, special files, unsafe ownership, and unsafe
  permissions. Directory traversal beneath the home directory uses directory file
  descriptors and no-follow opens. An atomic replacement and fsync preserve the
  previous state if writing is interrupted.
- The JSON schema and sizes are checked. Terminal controls and invisible format
  characters are removed from brew-watch's own package/error output, including
  command-line parser errors in the tool and installer. Locks prevent
  simultaneous scans or upgrades across terminals, and failed checks retain the
  last successful result.
- Setup is explicit, repeatable, and reversible. It backs up `.zshrc`, preserves
  unrelated bytes, revalidates contents and file safety immediately before
  replacement, and refuses unrelated files at its installation paths. Detected
  concurrent edits abort setup/removal without replacing the edited file.
  Development tests never activate the real hook.

## Trust limits

The user's shell configuration, PATH, Python installation, and selected Homebrew
installation must be trusted. brew-watch is not setuid and rejects root execution.
Processes running as the same user can alter the program or data; mode bits cannot
isolate applications from other processes belonging to that same account. Homebrew
may execute its own configuration, tap logic, downloads, and package installer code.
brew-watch does not validate or sandbox those internals. Homebrew itself may request
administrator permission during a user-requested cask upgrade.

Close editors of `.zshrc` before setup/removal. The installer lock cannot exclude
an unrelated editor writing between the final check and atomic replacement.
Backups contain the version read at setup start, not every concurrent edit.
Process cleanup covers the captured command's process group; a descendant that
deliberately creates a separate session is outside that boundary.

Homebrew's normal interactive output is shown during explicit upgrades. Output
sanitization applies to brew-watch's own captured reports, not to the trusted
interactive Homebrew process. Manual upgrades may partially succeed before failing;
there is no automatic rollback. There is no warranty that every upgrade is compatible
with the user's projects or databases.

These safeguards reduce specific risks; they are not a security certification.
See the README for the exact tested and untested boundaries. Report a suspected
issue privately to the maintainer through the repository host's private security
reporting feature if available. Do not include credentials or private package data
in a public issue.
