#!/usr/bin/env python3
"""A terminal reminder for Homebrew. Python standard library only."""

import argparse
import contextlib
import ctypes
import errno
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
_acl = None


class Error(Exception):
    pass


def safe_text(value):
    return "".join(c for c in str(value) if c.isprintable() and not unicodedata.category(c).startswith("C"))


class ArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        super().error(safe_text(message))


def check_acl(fd, private=False, reject_acl=False):
    """Reject unsafe macOS ACL grants, including inherited entries."""
    global _acl
    if sys.platform != "darwin":
        raise Error("Permission validation requires macOS.")
    if _acl is None:
        library = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
        pointer = ctypes.c_void_p
        for name, args, result in (
            ("acl_get_fd_np", [ctypes.c_int, ctypes.c_int], pointer),
            ("acl_get_entry", [pointer, ctypes.c_int, ctypes.POINTER(pointer)], ctypes.c_int),
            ("acl_get_tag_type", [pointer, ctypes.POINTER(ctypes.c_int)], ctypes.c_int),
            ("acl_get_permset", [pointer, ctypes.POINTER(pointer)], ctypes.c_int),
            ("acl_get_perm_np", [pointer, ctypes.c_int], ctypes.c_int),
            ("acl_free", [pointer], ctypes.c_int),
        ):
            function = getattr(library, name)
            function.argtypes, function.restype = args, result
        _acl = library
    acl = _acl.acl_get_fd_np(fd, 0x100)  # ACL_TYPE_EXTENDED in sys/acl.h.
    if not acl:
        if ctypes.get_errno() == errno.ENOENT:  # No extended ACL on this open file.
            return
        raise Error("Unable to validate file ACL permissions; refusing access.")
    try:
        entry, permissions, tag = ctypes.c_void_p(), ctypes.c_void_p(), ctypes.c_int()
        which = 0  # ACL_FIRST_ENTRY; ACL_NEXT_ENTRY is -1 on macOS.
        while True:
            result = _acl.acl_get_entry(acl, which, ctypes.byref(entry))
            if result == -1:
                # Darwin uses EINVAL for the end of an ACL, including an empty ACL.
                if ctypes.get_errno() == errno.EINVAL:
                    break
                raise Error("Unable to read file ACL permissions; refusing access.")
            which = -1
            if reject_acl:
                raise Error("Refusing to replace .zshrc with an existing ACL; its access restrictions must be preserved.")
            if (_acl.acl_get_tag_type(entry, ctypes.byref(tag)) != 0
                    or _acl.acl_get_permset(entry, ctypes.byref(permissions)) != 0):
                raise Error("Unable to read file ACL permissions; refusing access.")
            if tag.value == 1:  # ACL_EXTENDED_ALLOW; deny entries cannot grant access.
                # Mutation rights; private files also reject read/search grants.
                bits = range(1, 14) if private else (2, 4, 5, 6, 8, 10, 12, 13)
                if any(_acl.acl_get_perm_np(permissions, 1 << bit) != 0 for bit in bits):
                    raise Error("Unsafe ACL permissions; refusing access.")
    finally:
        _acl.acl_free(acl)


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
        check_acl(fd)
        for index, part in enumerate(parts):
            try:
                os.mkdir(part, 0o700, dir_fd=fd)
            except FileExistsError:
                pass
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | NOFOLLOW, dir_fd=fd)
            try:
                check_stat(os.fstat(child), directory=True,
                           private=private and index == len(parts) - 1)
                check_acl(child, private=private and index == len(parts) - 1)
            except BaseException:
                os.close(child)
                raise
            os.close(fd)
            fd = child
        yield fd
    finally:
        os.close(fd)


def read_file(fd, name, private=True, reject_acl=False):
    try:
        source = os.open(name, os.O_RDONLY | os.O_NONBLOCK | NOFOLLOW, dir_fd=fd)
    except FileNotFoundError:
        return None
    with os.fdopen(source, "rb") as stream:
        check_stat(os.fstat(stream.fileno()), private=private)
        check_acl(stream.fileno(), private=private, reject_acl=reject_acl)
        data = stream.read(LIMIT + 1)
    if len(data) > LIMIT:
        raise Error("File exceeds the size limit.")
    return data


def write_file(fd, name, data, mode=0o600, *, expected=..., reject_acl=False):
    read_file(fd, name, private=False, reject_acl=reject_acl)
    temporary = ".brew-watch-" + secrets.token_hex(16)
    target = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | NOFOLLOW, mode, dir_fd=fd)
    try:
        with os.fdopen(target, "wb") as stream:
            check_acl(stream.fileno(), private=not mode & 0o077)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        current = read_file(fd, name, private=False, reject_acl=reject_acl)
        if expected is not ... and current != expected:
            raise Error("Your .zshrc changed during setup; retry after saving your edits.")
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
        check_acl(lock, private=True)
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
        # Bound timestamps to year 9999, leaving room to display the next due date.
        for name in ("last_check", "last_attempt"):
            value = state[name]
            if value is not None and (type(value) is not int or not 0 <= value <= 253402300799 - INTERVAL):
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
    # Leave space to record a later failure without discarding the last good result.
    if len(data) > LIMIT - (2048 if state["error"] is None else 0):
        raise Error("Package report exceeds the state size limit.")
    write_file(fd, "state.json", data)


def homebrew():
    found = shutil.which("brew")
    if not found:
        raise Error("Homebrew was not found on PATH. Load Homebrew before the brew-watch shell hook.")
    path = Path(found).absolute()
    parents = set()

    def resolve(candidate):
        current = Path(candidate.anchor)
        pending = list(candidate.parts[1:])
        links = 0
        while pending:
            parents.add(current)
            part = pending.pop(0)
            if part == "..":
                current = current.parent
                continue
            child = current / part
            if stat.S_ISLNK(child.lstat().st_mode):
                links += 1
                if links > 40:
                    raise Error("Too many symlinks in the Homebrew path.")
                target = child.readlink()
                if target.is_absolute():
                    current = Path(target.anchor)
                    pending = list(target.parts[1:]) + pending
                else:
                    pending = list(target.parts) + pending
            else:
                current = child
        return current

    # Retain every traversed directory, including intermediate symlink locations.
    executable_directory = resolve(path.parent)
    if ":" in str(executable_directory):
        raise Error("The Homebrew executable directory cannot contain a PATH separator.")
    real = resolve(executable_directory / path.name)
    for parent in sorted(parents, key=lambda item: len(item.parts)):
        fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | NOFOLLOW)
        try:
            info = os.fstat(fd)
            if info.st_uid not in (0, os.getuid()) or info.st_mode & 0o022:
                raise Error("A Homebrew parent directory has unsafe ownership or permissions.")
            check_acl(fd)
        finally:
            os.close(fd)
    fd = os.open(real, os.O_RDONLY | os.O_NONBLOCK | NOFOLLOW)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid not in (0, os.getuid()) or info.st_mode & 0o022:
            raise Error("The Homebrew executable has unsafe ownership or permissions.")
        check_acl(fd)
    finally:
        os.close(fd)
    # Keep proxy/certificate settings, but never inherit executable-injection variables.
    allowed = {"LANG", "LC_ALL", "LC_CTYPE", "TMPDIR", "USER", "LOGNAME",
               "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY", "http_proxy",
               "https_proxy", "all_proxy", "no_proxy", "SSL_CERT_FILE", "SSL_CERT_DIR"}
    env = {key: value for key, value in os.environ.items() if key in allowed}
    env.update(HOME=str(Path.home()), PATH=str(executable_directory) + ":/usr/bin:/bin:/usr/sbin:/sbin",
               HOMEBREW_NO_ANALYTICS="1", HOMEBREW_NO_AUTO_UPDATE="1",
               HOMEBREW_FORCE_API_AUTO_UPDATE="1", HOMEBREW_NO_ENV_HINTS="1")
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


def capture(brew, env, args, timeout=60):
    process = None
    interrupted = False
    stopping = False

    def on_signal(signum, frame):
        nonlocal interrupted
        already_interrupted = interrupted
        interrupted = True
        # Defer during spawn; repeated signals must not interrupt group cleanup.
        if process is not None and not stopping and not already_interrupted:
            raise KeyboardInterrupt

    previous = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)}
    output = bytearray()
    size = 0
    deadline = time.monotonic() + timeout
    try:
        for sig in previous:
            signal.signal(sig, on_signal)
        process = subprocess.Popen([brew, *args], stdin=subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   cwd=Path.home(), env=env, start_new_session=True)
        if interrupted:
            raise KeyboardInterrupt
        with selectors.DefaultSelector() as selector:
            for pipe in (process.stdout, process.stderr):
                os.set_blocking(pipe.fileno(), False)
                selector.register(pipe, selectors.EVENT_READ)
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise Error("Homebrew check timed out.")
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
                raise Error("Homebrew check timed out.") from None
        if result:
            raise Error(f"Homebrew {args[0]} failed (exit {result}); see brew-watch status.")
        return bytes(output)
    except BaseException:
        stopping = True
        if process is not None:
            stop(process)
        raise
    finally:
        stopping = True
        if process is not None:
            process.stdout.close()
            process.stderr.close()
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def scan(brew, env):
    return packages_from_json(capture(brew, env, ["outdated", "--json=v2"]))


def report(packages):
    if not packages:
        return
    print("\nThese brews could use a little attention:\n")
    print(f"Preview of {len(packages)} packages: up to 20 entries; long lines shortened.\n")
    for package in packages[:20]:
        pinned = " [pinned; Homebrew will skip it]" if package["pinned"] else ""
        line = safe_text(f"  {package['kind']}: {package['name']}  {package['installed']} -> {package['available']}{pinned}")
        print(line[:237] + "..." if len(line) > 240 else line)
    print("\nInspect them with Homebrew: brew outdated --verbose")
    print("Uses Homebrew metadata; third-party taps may be stale.")
    print("brew-watch did not request package upgrades or installations.\n")


def timestamp(value):
    return time.strftime("%Y-%m-%d %H:%M %Z", time.localtime(value))


def status(state):
    checked = state["last_check"]
    now = time.time()
    print("Last successful check:", timestamp(checked) if checked is not None else "never")
    print("Metadata: API refresh enabled; third-party taps use local definitions. Cached data may be used.")
    print("Packages with updates at that check:", len(state["packages"]) if checked is not None else "unknown")
    if any(state[key] is not None and state[key] > now
           for key in ("last_check", "last_attempt")):
        print("Clock discrepancy: saved history is ahead of the system clock. "
              "Automatic checks wait for the saved check/retry interval; check your system clock.")
    due = checked + INTERVAL if checked is not None else 0
    if state["last_attempt"] is not None:
        due = max(due, state["last_attempt"] + RETRY)
    print("Next check:", "due now, on your next terminal opening" if due <= now
          else f"due {timestamp(due)}, on your next terminal opening")
    if state["error"]:
        print("Last attempt:", timestamp(state["last_attempt"]))
        print("Check error:", safe_text(state["error"]))
        retry = state["last_attempt"] + RETRY
        if retry > now:
            print("Automatic retry eligible:", timestamp(retry))
    print("Automatic reminders require the zsh hook; no background schedule is installed.")


def operate(command, fd):
    state = load_state(fd)
    if command == "status":
        status(state)
        return 0
    now = int(time.time())
    if state["last_check"] is not None and now - state["last_check"] < INTERVAL:
        return 0
    if state["last_attempt"] is not None and now - state["last_attempt"] < RETRY:
        return 0
    state["last_attempt"] = now
    save_state(fd, state)
    try:
        brew, env = homebrew()
        candidate = dict(state, packages=scan(brew, env), last_check=int(time.time()), error=None)
        save_state(fd, candidate)
        state = candidate
        # shortcut: closing this terminal can lose the notice; add delivery acknowledgement if needed.
        report(state["packages"])
        return 0
    except (Error, OSError) as error:
        message = str(error) if isinstance(error, Error) else "Unable to run Homebrew or save the result."
        state["error"] = message
        save_state(fd, state)
        raise Error(message) from None


def main(argv=None):
    parser = ArgumentParser(prog="brew-watch", description="Check Homebrew on terminal opening, at most once every 14 days.",
                                     epilog="The automatic check runs only brew outdated --json=v2, with API metadata refresh enabled. "
                                     "brew-watch never directly requests package upgrades or installations. "
                                     "It does not run brew update or refresh third-party taps. Homebrew may use cached metadata. "
                                     "See README.md for shell setup and removal.")
    parser.color = False  # Python 3.14+; harmless on older supported interpreters.
    parser.add_argument("command", choices=("status", "_startup"), metavar="{status}",
                        help="status: show the last result and next check without contacting Homebrew")
    args = parser.parse_args(argv)
    try:
        if os.getuid() == 0 or os.geteuid() != os.getuid():
            raise Error("Run brew-watch as your normal user, never with sudo or elevated privileges.")
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
