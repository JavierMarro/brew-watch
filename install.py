#!/usr/bin/env python3
"""Explicit, reversible local setup. Does not run Homebrew."""

import argparse
import contextlib
import os
from pathlib import Path
import runpy
import secrets
import shlex
import sys

ROOT = Path(__file__).resolve().parent
API = runpy.run_path(str(ROOT / "brew_watch.py"))
Error, directory, read_file, write_file = (API[name] for name in ("Error", "directory", "read_file", "write_file"))
MARKER = b"# Managed by brew-watch installer.\n"
BEGIN = b"# >>> brew-watch >>>\n"
END = b"# <<< brew-watch <<<\n"


def remove_block(data):
    if BEGIN not in data and END not in data:
        return data
    if data.count(BEGIN) != 1 or data.count(END) != 1:
        raise Error("Ambiguous brew-watch startup block; inspect your .zshrc manually.")
    start = data.index(BEGIN)
    end_start = data.index(END)
    end = end_start + len(END)
    if (start == 0 or data[start - 1:start] != b"\n"
            or end_start < start + len(BEGIN) or data[end_start - 1:end_start] != b"\n"):
        raise Error("Modified brew-watch startup block; inspect your .zshrc manually.")
    return data[:start - 1] + data[end:]


def setup(uninstall=False):
    if os.getuid() == 0 or os.geteuid() != os.getuid():
        raise Error("Run setup as your normal user, never with sudo.")
    home = Path.home()
    destination = home / ".local/share/brew-watch"
    binary = home / ".local/bin/brew-watch"
    zsh_directory = Path(os.environ.get("ZDOTDIR", str(home)))
    if not zsh_directory.is_absolute():
        raise Error("ZDOTDIR must be an absolute directory inside your home.")
    hook = destination / "startup.zsh"
    wrapper = ("#!/bin/sh\n" + MARKER.decode() + "exec " + shlex.quote(sys.executable)
               + " -I " + shlex.quote(str(destination / "brew_watch.py")) + ' "$@"\n').encode()
    files = ((destination, "brew_watch.py", MARKER + (ROOT / "brew_watch.py").read_bytes(), 0o600),
             (destination, "startup.zsh", MARKER + (ROOT / "shell/brew-watch.zsh").read_bytes(), 0o600),
             (binary.parent, binary.name, wrapper, 0o700))
    with contextlib.ExitStack() as stack:
        install_fd = stack.enter_context(directory(destination, private=True))
        stack.enter_context(API["locked"](install_fd))
        zfd = stack.enter_context(directory(zsh_directory))
        existing = read_file(zfd, ".zshrc", private=False)
        original = existing or b""
        cleaned = remove_block(original)
        if uninstall:
            updated = cleaned
        else:
            block = ("\ntypeset -g _BREW_WATCH_COMMAND=" + shlex.quote(str(binary))
                     + "\nfunction brew-watch() { command \"$_BREW_WATCH_COMMAND\" \"$@\"; }\nsource "
                     + shlex.quote(str(hook)) + "\n").encode()
            updated = cleaned + b"\n" + BEGIN + block + END
        # Validate every destination before changing startup or installed files.
        for parent, name, _, _ in files:
            with directory(parent, private=parent == destination) as fd:
                current = read_file(fd, name, private=False)
                if current is not None and MARKER not in current[:256]:
                    raise Error("An unrelated file occupies a brew-watch installation path; refusing to overwrite it.")
        if not uninstall:
            for parent, name, contents, mode in files:
                with directory(parent, private=parent == destination) as fd:
                    write_file(fd, name, contents, mode)
        if updated != original:
            if read_file(zfd, ".zshrc", private=False) != existing:
                raise Error("Your .zshrc changed during setup; retry after saving your edits.")
            if existing is not None:
                backup = ".zshrc.brew-watch-backup-" + secrets.token_hex(12)
                write_file(zfd, backup, original)
                mode = os.stat(".zshrc", dir_fd=zfd, follow_symlinks=False).st_mode & 0o777
                print("Preserved startup backup:", API["safe_text"](str(zsh_directory / backup)))
            else:
                mode = 0o600
            write_file(zfd, ".zshrc", updated, mode)
        if uninstall:
            for parent, name, _, _ in files:
                with directory(parent, private=parent == destination) as fd:
                    if read_file(fd, name, private=False) is not None:
                        os.unlink(name, dir_fd=fd)
    print("brew-watch removed. Open a new terminal. Private scan history is retained." if uninstall
          else "brew-watch installed. Open a new terminal; the first check runs there.")


def main():
    parser = argparse.ArgumentParser(description="Install brew-watch locally and add a marked .zshrc block. Never runs Homebrew.")
    parser.add_argument("--uninstall", action="store_true", help="remove the marked block and only managed program files")
    args = parser.parse_args()
    try:
        setup(args.uninstall)
        return 0
    except (Error, OSError) as error:
        message = str(error) if isinstance(error, Error) else "Unsafe or inaccessible installation files; setup stopped."
        print("brew-watch: " + API["safe_text"](message), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
