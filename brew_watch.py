#!/usr/bin/env python3
"""A terminal reminder for Homebrew. Python standard library only."""

import argparse
import contextlib
import fcntl
import json
import os
from pathlib import Path
import re
import secrets
import selectors
import shutil
import signal
import stat
import subprocess
import sys
import time
import unicodedata

INTERVAL = 14 * 24 * 60 * 60
RETRY = 60 * 60
LIMIT = 1024 * 1024
NOFOLLOW = os.O_NOFOLLOW | os.O_CLOEXEC


class Error(Exception):
    pass


def safe_text(value):
    return "".join(c for c in str(value) if c.isprintable() and not unicodedata.category(c).startswith("C"))


def check_stat(info, directory=False, private=False):
    valid_type = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
    if (not valid_type or info.st_uid != os.getuid()
            or info.st_mode & (0o077 if private else 0o022)
            or (not directory and info.st_nlink != 1)):
        raise Error("Unsafe file ownership, type, or permissions; refusing access.")


@contextlib.contextmanager
def directory(path, private=False):
    """Walk beneath the trusted home directory without following symlinks."""
    home = Path.home().absolute()
    try:
        parts = Path(path).absolute().relative_to(home).parts
    except ValueError:
        raise Error("brew-watch files must be inside your home directory.") from None
    if any(part in (".", "..") for part in parts):
        raise Error("Invalid file path.")
    fd = os.open(home, os.O_RDONLY | os.O_DIRECTORY | NOFOLLOW)
    try:
        check_stat(os.fstat(fd), directory=True)
        for index, part in enumerate(parts):
            try:
                os.mkdir(part, 0o700, dir_fd=fd)
            except FileExistsError:
                pass
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | NOFOLLOW, dir_fd=fd)
            try:
                check_stat(os.fstat(child), directory=True,
                           private=private and index == len(parts) - 1)
            except BaseException:
                os.close(child)
                raise
            os.close(fd)
            fd = child
        yield fd
    finally:
        os.close(fd)


def read_file(fd, name, private=True):
    try:
        source = os.open(name, os.O_RDONLY | os.O_NONBLOCK | NOFOLLOW, dir_fd=fd)
    except FileNotFoundError:
        return None
    with os.fdopen(source, "rb") as stream:
        check_stat(os.fstat(stream.fileno()), private=private)
        data = stream.read(LIMIT + 1)
    if len(data) > LIMIT:
        raise Error("File exceeds the size limit.")
    return data


def write_file(fd, name, data, mode=0o600):
    read_file(fd, name, private=False)
    temporary = ".brew-watch-" + secrets.token_hex(16)
    target = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | NOFOLLOW, mode, dir_fd=fd)
    try:
        with os.fdopen(target, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, name, src_dir_fd=fd, dst_dir_fd=fd)
        os.fsync(fd)
    finally:
        try:
            os.unlink(temporary, dir_fd=fd)
        except FileNotFoundError:
            pass


@contextlib.contextmanager
def locked(fd):
    lock = os.open("lock", os.O_RDWR | os.O_CREAT | os.O_NONBLOCK | NOFOLLOW, 0o600, dir_fd=fd)
    try:
        check_stat(os.fstat(lock), private=True)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Error("Another brew-watch operation is running.") from None
        yield
    finally:
        os.close(lock)


def packages_from_json(raw):
    try:
        data = json.loads(raw)
        packages = []
        for key, kind in (("formulae", "Formula"), ("casks", "Cask")):
            rows = data[key]
            if not isinstance(rows, list) or len(rows) > 10000:
                raise ValueError
            for row in rows:
                name, installed, available = row["name"], row["installed_versions"], row["current_version"]
                if (not isinstance(name, str) or len(name) > 256
                        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9@+_./-]*", name)
                        or not isinstance(installed, list) or not installed
                        or len(installed) > 100
                        or any(not isinstance(v, str) or len(v) > 256 for v in installed)
                        or not isinstance(available, str) or len(available) > 256
                        or type(row.get("pinned", False)) is not bool):
                    raise ValueError
                packages.append({"kind": kind, "name": name, "installed": ", ".join(map(safe_text, installed)),
                                 "available": safe_text(available), "pinned": row.get("pinned", False)})
        return packages
    except (ValueError, KeyError, TypeError, RecursionError):
        raise Error("Homebrew returned invalid package data.") from None


def empty_state():
    return {"last_check": None, "last_attempt": None, "packages": [], "error": None}


def load_state(fd):
    raw = read_file(fd, "state.json")
    if raw is None:
        return empty_state()
    try:
        state = json.loads(raw)
        if not isinstance(state, dict) or set(state) != set(empty_state()):
            raise ValueError
        now = time.time()
        for name in ("last_check", "last_attempt"):
            value = state[name]
            if value is not None and (type(value) is not int or not 0 <= value <= now + 300):
                raise ValueError
        if state["error"] is not None and (not isinstance(state["error"], str) or len(state["error"]) > 256):
            raise ValueError
        if state["error"] is not None and state["last_attempt"] is None:
            raise ValueError
        rows = state["packages"]
        if not isinstance(rows, list) or len(rows) > 20000:
            raise ValueError
        for row in rows:
            if (not isinstance(row, dict) or set(row) != {"kind", "name", "installed", "available", "pinned"}
                    or row["kind"] not in ("Formula", "Cask") or type(row["pinned"]) is not bool
                    or any(not isinstance(row[key], str) or len(row[key]) > 26000
                           for key in ("name", "installed", "available"))):
                raise ValueError
        return state
    except (ValueError, TypeError, KeyError, RecursionError):
        raise Error("State is corrupt; inspect or remove only brew-watch's state.json to reset it.") from None


def save_state(fd, state):
    data = json.dumps(state, ensure_ascii=True).encode()
    if len(data) > LIMIT:
        raise Error("Package report exceeds the state size limit.")
    write_file(fd, "state.json", data)


def homebrew():
    found = shutil.which("brew")
    if not found:
        raise Error("Homebrew was not found on PATH. Load Homebrew before the brew-watch shell hook.")
    path = Path(found).absolute()
    real = path.resolve(strict=True)
    info = real.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid not in (0, os.getuid()) or info.st_mode & 0o022:
        raise Error("The Homebrew executable has unsafe ownership or permissions.")
    # Keep proxy/certificate settings, but never inherit executable-injection variables.
    allowed = {"LANG", "LC_ALL", "LC_CTYPE", "TMPDIR", "USER", "LOGNAME",
               "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY", "http_proxy",
               "https_proxy", "all_proxy", "no_proxy", "SSL_CERT_FILE", "SSL_CERT_DIR"}
    env = {key: value for key, value in os.environ.items() if key in allowed}
    env.update(HOME=str(Path.home()), PATH=str(path.parent) + ":/usr/bin:/bin:/usr/sbin:/sbin",
               HOMEBREW_NO_ANALYTICS="1", HOMEBREW_NO_AUTO_UPDATE="1", HOMEBREW_NO_ENV_HINTS="1")
    return str(real), env


def stop(process):
    try:
        os.killpg(process.pid, signal.SIGTERM)
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass
        # A child may outlive its parent; terminate the whole background group.
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait()


def capture(brew, env, args, timeout=120):
    process = subprocess.Popen([brew, *args], stdin=subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               cwd=Path.home(), env=env, start_new_session=True)
    output = bytearray()
    size = 0
    deadline = time.monotonic() + timeout
    try:
        with selectors.DefaultSelector() as selector:
            for pipe in (process.stdout, process.stderr):
                os.set_blocking(pipe.fileno(), False)
                selector.register(pipe, selectors.EVENT_READ)
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise Error("Homebrew check timed out; no packages were upgraded.")
                for key, _ in selector.select(min(remaining, 1)):
                    chunk = os.read(key.fd, 65536)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    size += len(chunk)
                    if size > LIMIT:
                        raise Error("Homebrew output exceeds the size limit.")
                    if key.fileobj is process.stdout:
                        output.extend(chunk)
            try:
                result = process.wait(timeout=max(0.001, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                raise Error("Homebrew check timed out; no packages were upgraded.") from None
        if result:
            raise Error(f"Homebrew {args[0]} failed (exit {result}); see brew-watch status.")
        return bytes(output)
    except BaseException:
        stop(process)
        raise
    finally:
        process.stdout.close()
        process.stderr.close()


def scan(brew, env, refresh=True):
    if refresh:
        capture(brew, env, ["update"])
    return packages_from_json(capture(brew, env, ["outdated", "--json=v2"], timeout=60))


def report(packages):
    if not packages:
        return
    print("\nThese brews could use a little attention:\n")
    for package in packages:
        pinned = " [pinned; Homebrew will skip it]" if package["pinned"] else ""
        print(safe_text(f"  {package['kind']}: {package['name']}  {package['installed']} -> {package['available']}{pinned}"))
    print("\nFreshen eligible packages: brew-watch freshen")
    print("Or inspect them: brew outdated --verbose")
    print("No packages have been upgraded.\n")


def timestamp(value):
    return time.strftime("%Y-%m-%d %H:%M %Z", time.localtime(value))


def status(state):
    checked = state["last_check"]
    print("Last successful check:", timestamp(checked) if checked is not None else "never")
    print("Packages with updates at that check:", len(state["packages"]) if checked is not None else "unknown")
    due = checked + INTERVAL if checked is not None else 0
    print("Next check:", "due now, on your next terminal opening" if due <= time.time()
          else f"due {timestamp(due)}, on your next terminal opening")
    if state["error"]:
        print("Last attempt:", timestamp(state["last_attempt"]))
        print("Check error:", safe_text(state["error"]))
        retry = state["last_attempt"] + RETRY
        if retry > time.time():
            print("Automatic retry eligible:", timestamp(retry))
    print("Automatic reminders require the zsh hook; no background schedule is installed.")


def operate(command, fd):
    state = load_state(fd)
    if command == "status":
        status(state)
        return 0
    now = int(time.time())
    if command == "_startup":
        if state["last_check"] is not None and now - state["last_check"] < INTERVAL:
            return 0
        if state["last_attempt"] is not None and now - state["last_attempt"] < RETRY:
            return 0
    state["last_attempt"] = now
    save_state(fd, state)
    try:
        brew, env = homebrew()
        state["packages"] = scan(brew, env)
        state["last_check"] = int(time.time())
        state["error"] = None
        save_state(fd, state)
        # shortcut: closing this terminal can lose the notice; add delivery acknowledgement if needed.
        report(state["packages"])
        if command == "freshen":
            if not state["packages"]:
                print("Your brews are up to date.")
                return 0
            # The caller explicitly requested upgrades; Homebrew owns its prompts and output.
            result = subprocess.run([brew, "upgrade", "--ask"], cwd=Path.home(), env=env).returncode
            state["packages"] = scan(brew, env, refresh=False)
            state["last_check"] = int(time.time())
            save_state(fd, state)
            if result:
                raise Error(f"Homebrew upgrade failed (exit {result}); some packages may have changed.")
            print("Freshening complete. Packages still reported outdated:", len(state["packages"]))
        return 0
    except (Error, OSError) as error:
        message = str(error) if isinstance(error, Error) else "Unable to run Homebrew or save the result."
        state["error"] = message
        save_state(fd, state)
        raise Error(message) from None


def main(argv=None):
    parser = argparse.ArgumentParser(description="Check Homebrew on terminal opening, at most once every 14 days.",
                                     epilog="The automatic check refreshes Homebrew itself and metadata, never installed packages. "
                                     "freshen refreshes, previews, and upgrades eligible formulae and casks using Homebrew. "
                                     "See README.md for shell setup and removal.")
    parser.add_argument("command", choices=("status", "freshen", "_startup"), metavar="{status,freshen}",
                        help="status: last result and next check; freshen: explicitly upgrade packages")
    args = parser.parse_args(argv)
    try:
        if os.getuid() == 0 or os.geteuid() != os.getuid():
            raise Error("Run brew-watch as your normal user, never with sudo or elevated privileges.")
        if args.command == "freshen" and not (sys.stdin.isatty() and sys.stdout.isatty()):
            raise Error("freshen requires an interactive terminal; no upgrades were started.")
        if args.command == "_startup":
            def interrupted(signum, frame):
                raise KeyboardInterrupt
            signal.signal(signal.SIGTERM, interrupted)
            signal.signal(signal.SIGHUP, interrupted)
        with directory(Path.home() / ".local/state/brew-watch", private=True) as fd:
            if args.command == "status":
                return operate(args.command, fd)
            with locked(fd):
                return operate(args.command, fd)
    except (Error, OSError) as error:
        message = str(error) if isinstance(error, Error) else "Unsafe or inaccessible brew-watch files; refusing access."
        if args.command == "_startup" and message == "Another brew-watch operation is running.":
            return 0
        print("brew-watch: " + safe_text(message), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nbrew-watch: interrupted.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
