import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import pty
import select
import shlex
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


bw = module("brew_watch", ROOT / "brew_watch.py")
installer = module("installer", ROOT / "install.py")

FAKE_BREW = r'''
import json, os, signal, subprocess, sys, time
from pathlib import Path
base = Path(__file__).parent
config = json.loads((base / "config.json").read_text())
args = sys.argv[1:]
with (base / "calls.jsonl").open("a") as log:
    log.write(json.dumps({"args": args, "env": dict(os.environ)}) + "\n")
if config.get("tree"):
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    child = subprocess.Popen([sys.executable, "-I", "-c",
        'import os, signal, sys, time; from pathlib import Path; '
        'signal.signal(signal.SIGTERM, signal.SIG_IGN); '
        'Path(sys.argv[1]).write_text(str(os.getpid())); time.sleep(30)', str(base / "child-pid")])
    (base / "pid").write_text(str(os.getpid()))
    if config.get("overflow"):
        while not (base / "child-pid").exists():
            time.sleep(0.01)
        sys.stdout.write("x" * (1024 * 1024 + 1))
        sys.stdout.flush()
    child.wait()
if config.get("sleep"):
    (base / "pid").write_text(str(os.getpid()))
    time.sleep(config["sleep"])
if config.get("overflow"):
    sys.stdout.write("x" * (1024 * 1024 + 1))
    sys.stdout.flush()
    time.sleep(20)
if args[0] == config.get("fail"):
    print("SECRET_TOKEN=never-display-this\x1b[2J", file=sys.stderr)
    sys.exit(7)
if args[0] == "outdated":
    sys.stdout.write(json.dumps(config["reply"]))
'''


class BrewWatchTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="brew-watch-test-")
        self.addCleanup(self.temporary.cleanup)
        self.home = Path(self.temporary.name)
        self.bin = self.home / "fake-bin"
        self.bin.mkdir(mode=0o700)
        self.brew = self.bin / "brew"
        self.brew.write_text(f"#!{sys.executable} -I\n" + FAKE_BREW)
        self.brew.chmod(0o700)
        self.config = {"reply": {"formulae": [{"name": "example-tool", "installed_versions": ["1.0"],
                                              "current_version": "2.0", "pinned": False}],
                                 "casks": [{"name": "example-app", "installed_versions": ["3.0"],
                                            "current_version": "4.0"}]}}
        self.configure()
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(Path, "home", return_value=self.home).start()
        self.env = {"HOME": str(self.home), "PATH": str(self.bin) + ":/usr/bin:/bin", "LANG": "en_US.UTF-8"}
        mock.patch.dict(os.environ, self.env, clear=True).start()

    def configure(self):
        (self.bin / "config.json").write_text(json.dumps(self.config))

    def calls(self):
        path = self.bin / "calls.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def run_command(self, command):
        output, error = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(error):
            result = bw.main([command])
        return result, output.getvalue(), error.getvalue()

    def state(self):
        return json.loads((self.home / ".local/state/brew-watch/state.json").read_text())

    def test_startup_checks_once_and_covers_formulae_and_casks(self):
        result, output, error = self.run_command("_startup")
        self.assertEqual(result, 0, error)
        self.assertIn("Formula: example-tool", output)
        self.assertIn("Cask: example-app", output)
        self.assertIn("brew-watch did not request package upgrades or installations", output)
        self.assertEqual([call["args"] for call in self.calls()], [["update"], ["outdated", "--json=v2"]])
        self.assertEqual(self.run_command("_startup"), (0, "", ""))
        self.assertEqual(len(self.calls()), 2)
        self.assertEqual(len(self.state()["packages"]), 2)

    def test_due_check_and_status_are_honest_about_freshness(self):
        self.run_command("_startup")
        with mock.patch.object(bw.time, "time", return_value=time.time() + bw.INTERVAL + 1):
            self.assertEqual(self.run_command("_startup")[0], 0)
            result, output, _ = self.run_command("status")
            self.assertEqual(result, 0)
            self.assertIn("on your next terminal opening", output)
            self.assertIn("Packages with updates at that check: 2", output)
        self.assertEqual(len(self.calls()), 4)

    def test_backward_clock_preserves_history_and_resumes_after_interval(self):
        for failed in (False, True):
            with self.subTest(failed=failed):
                path = self.home / ".local/state/brew-watch/state.json"
                path.unlink(missing_ok=True)
                self.config["fail"] = "update" if failed else None
                self.configure()
                saved_time = int(time.time())
                with mock.patch.object(bw.time, "time", return_value=saved_time):
                    self.assertEqual(self.run_command("_startup")[0], 1 if failed else 0)
                original = path.read_bytes()
                calls = len(self.calls())
                interval = bw.RETRY if failed else bw.INTERVAL
                for now in (saved_time - 3600, saved_time + interval - 1):
                    with mock.patch.object(bw.time, "time", return_value=now):
                        result, output, error = self.run_command("status")
                        self.assertEqual(result, 0, error)
                        if now < saved_time:
                            self.assertIn("Clock discrepancy", output)
                            self.assertNotIn("due now", output)
                        self.assertEqual(self.run_command("_startup"), (0, "", ""))
                    self.assertEqual(path.read_bytes(), original)
                    self.assertEqual(len(self.calls()), calls)
                self.config["fail"] = None
                self.configure()
                with mock.patch.object(bw.time, "time", return_value=saved_time + interval):
                    self.assertEqual(self.run_command("_startup")[0], 0)
                self.assertEqual(len(self.calls()), calls + 2)

    def test_timestamp_schema_still_rejects_invalid_values(self):
        with bw.directory(self.home / ".local/state/brew-watch", private=True) as fd:
            for key in ("last_check", "last_attempt"):
                for value in (-1, True, 1.5, "123", 10 ** 100):
                    with self.subTest(key=key, value=value):
                        state = dict(bw.empty_state(), **{key: value})
                        bw.write_file(fd, "state.json", json.dumps(state).encode())
                        with self.assertRaisesRegex(bw.Error, "corrupt"):
                            bw.load_state(fd)

    def test_status_does_not_invoke_homebrew(self):
        result, output, _ = self.run_command("status")
        self.assertEqual(result, 0)
        self.assertIn("never", output)
        self.assertIn("unknown", output)
        self.assertEqual(self.calls(), [])

    def test_failure_preserves_last_success_and_throttles_retries(self):
        self.run_command("_startup")
        before = self.state()
        self.config["fail"] = "update"
        self.configure()
        later = time.time() + bw.INTERVAL + 1
        with mock.patch.object(bw.time, "time", return_value=later):
            result, output, error = self.run_command("_startup")
            self.assertEqual(result, 1)
            self.assertNotIn("SECRET_TOKEN", error + output)
            self.assertNotIn("\x1b", error + output)
            self.assertEqual(self.state()["last_check"], before["last_check"])
            self.assertEqual(self.state()["packages"], before["packages"])
            self.assertEqual(self.run_command("_startup"), (0, "", ""))
        self.assertEqual([call["args"][0] for call in self.calls()], ["update", "outdated", "update"])

    def test_missing_brew_and_no_updates(self):
        with mock.patch.dict(os.environ, {"PATH": "/nonexistent"}):
            self.assertEqual(self.run_command("_startup")[0], 1)
        self.config["reply"] = {"formulae": [], "casks": []}
        self.configure()
        with mock.patch.object(bw.time, "time", return_value=time.time() + bw.RETRY + 1):
            self.assertEqual(self.run_command("_startup"), (0, "", ""))
        self.assertEqual(self.state()["packages"], [])

    def test_removed_upgrade_commands_are_rejected_before_any_work(self):
        for command in ("freshen", "upgrade", "install"):
            with self.subTest(command=command):
                with self.assertRaises(SystemExit) as result:
                    self.run_command(command)
                self.assertEqual(result.exception.code, 2)
        self.assertEqual(self.calls(), [])
        self.assertFalse((self.home / ".local").exists())

    def test_help_documents_reminder_only_commands_without_homebrew(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            with self.assertRaises(SystemExit) as result:
                bw.main(["--help"])
        self.assertEqual(result.exception.code, 0)
        self.assertIn("status", output.getvalue())
        self.assertIn("never directly requests package upgrades or installations", output.getvalue())
        self.assertNotIn("freshen", output.getvalue())
        self.assertNotIn("_startup", output.getvalue())
        self.assertEqual(self.calls(), [])
        self.assertFalse((self.home / ".local").exists())

    def test_invalid_package_json_preserves_last_success(self):
        self.assertEqual(self.run_command("_startup")[0], 0)
        previous = self.state()
        self.config["reply"] = {"formulae": "invalid", "casks": []}
        self.configure()
        with mock.patch.object(bw.time, "time", return_value=time.time() + bw.INTERVAL + 1):
            self.assertEqual(self.run_command("_startup")[0], 1)
        self.assertEqual(self.state()["last_check"], previous["last_check"])
        self.assertEqual(self.state()["packages"], previous["packages"])
        self.assertEqual([call["args"] for call in self.calls()],
                         [["update"], ["outdated", "--json=v2"]] * 2)

    def test_parser_errors_sanitize_options_and_program_names(self):
        hostile = "--bad\x1b[2J\r\n\x9b\u202e"
        for entry, args in ((bw.main, ["status", hostile]), (installer.main, [hostile])):
            with self.subTest(entry=entry.__module__):
                output = io.StringIO()
                with mock.patch.object(sys, "argv", [hostile, *args]), contextlib.redirect_stderr(output):
                    with self.assertRaises(SystemExit) as result:
                        entry()
                self.assertEqual(result.exception.code, 2)
                self.assertIn("unrecognized arguments", output.getvalue())
                for control in ("\x1b", "\r", "\x9b", "\u202e"):
                    self.assertNotIn(control, output.getvalue())
        self.assertEqual(self.calls(), [])
        self.assertFalse((self.home / ".zshrc").exists())

    def test_pinned_formula_is_labelled_without_force_or_greedy(self):
        self.config["reply"]["formulae"][0]["pinned"] = True
        self.configure()
        result, output, _ = self.run_command("_startup")
        self.assertEqual(result, 0)
        self.assertIn("pinned; Homebrew will skip it", output)
        self.assertFalse(any("--greedy" in call["args"] or "--force" in call["args"] for call in self.calls()))

    def test_terminal_controls_and_command_injection_are_not_interpreted(self):
        row = self.config["reply"]["formulae"][0]
        row["current_version"] = "2.0\x1b[2J\n\r\u202e"
        self.configure()
        result, output, error = self.run_command("_startup")
        self.assertEqual(result, 0, error)
        self.assertNotIn("\x1b", output)
        self.assertNotIn("\u202e", output)
        row["name"] = "$(touch pwned)"
        self.configure()
        with mock.patch.object(bw.time, "time", return_value=time.time() + bw.INTERVAL + 1):
            self.assertEqual(self.run_command("_startup")[0], 1)
        self.assertFalse((self.home / "pwned").exists())
        self.assertFalse(any(call["args"][0] == "upgrade" for call in self.calls()))

    def test_child_environment_drops_injection_and_never_bypasses_confirmation(self):
        with mock.patch.dict(os.environ, {"BASH_ENV": "evil", "RUBYOPT": "evil", "PYTHONPATH": "evil",
                                          "DYLD_INSERT_LIBRARIES": "evil", "HOMEBREW_NO_ASK": "1",
                                          "HTTPS_PROXY": "http://proxy.example"}):
            self.assertEqual(self.run_command("_startup")[0], 0)
        env = self.calls()[0]["env"]
        for key in ("BASH_ENV", "RUBYOPT", "PYTHONPATH", "DYLD_INSERT_LIBRARIES", "HOMEBREW_NO_ASK"):
            self.assertNotIn(key, env)
        self.assertEqual(env["HTTPS_PROXY"], "http://proxy.example")
        self.assertEqual(env["HOMEBREW_NO_ANALYTICS"], "1")

    def test_rejects_unsafe_brew(self):
        self.brew.chmod(0o777)
        self.assertEqual(self.run_command("_startup")[0], 1)
        self.assertEqual(self.calls(), [])

    def test_rejects_unsafe_brew_parents_and_symlink_locations(self):
        for mode in (0o770, 0o777):
            self.bin.chmod(mode)
            with self.assertRaisesRegex(bw.Error, "parent directory"):
                bw.homebrew()
        self.bin.chmod(0o700)
        links = self.home / "links"
        links.mkdir(mode=0o700)
        (links / "brew").symlink_to(self.brew)
        with mock.patch.dict(os.environ, {"PATH": str(links)}):
            self.assertEqual(bw.homebrew()[0], str(self.brew.resolve()))
            links.chmod(0o777)
            with self.assertRaisesRegex(bw.Error, "parent directory"):
                bw.homebrew()
            links.chmod(0o700)
            self.bin.chmod(0o777)
            with self.assertRaisesRegex(bw.Error, "parent directory"):
                bw.homebrew()
        self.assertEqual(self.calls(), [])

    @unittest.skipUnless(sys.platform == "darwin", "requires macOS ACLs")
    def test_acl_grants_are_rejected_on_state_lock_directories_and_brew(self):
        self.assertEqual(self.run_command("_startup")[0], 0)
        state = self.home / ".local/state/brew-watch/state.json"
        for path, grant, operation in (
            (state, "read", lambda: self.run_command("status")[0]),
            (state.parent / "lock", "write", lambda: self.run_command("_startup")[0]),
            (state.parent, "read", lambda: self.run_command("status")[0]),
            (self.home, "write", lambda: self.run_command("status")[0]),
        ):
            with self.subTest(path=path, grant=grant):
                subprocess.run(["/bin/chmod", "+a", f"everyone allow {grant}", str(path)], check=True)
                try:
                    self.assertEqual(operation(), 1)
                finally:
                    subprocess.run(["/bin/chmod", "-N", str(path)], check=True)
        for path in (self.brew, self.bin):
            subprocess.run(["/bin/chmod", "+a", "everyone allow write", str(path)], check=True)
            try:
                with self.assertRaisesRegex(bw.Error, "ACL"):
                    bw.homebrew()
            finally:
                subprocess.run(["/bin/chmod", "-N", str(path)], check=True)
        subprocess.run(["/bin/chmod", "+a", "everyone deny delete", str(self.home)], check=True)
        try:
            self.assertEqual(self.run_command("status")[0], 0)
        finally:
            subprocess.run(["/bin/chmod", "-N", str(self.home)], check=True)

    def test_brew_rejects_unsafe_intermediate_symlinks(self):
        shared = self.home / "shared"
        shared.mkdir(mode=0o700)
        (shared / "hop").symlink_to("../fake-bin", target_is_directory=True)
        alias = self.home / "alias"
        alias.symlink_to(shared / "hop", target_is_directory=True)
        with mock.patch.dict(os.environ, {"PATH": str(alias)}):
            self.assertEqual(bw.homebrew()[0], str(self.brew.resolve()))
            shared.chmod(0o777)
            with self.assertRaisesRegex(bw.Error, "parent directory"):
                bw.homebrew()
            shared.chmod(0o700)
            subprocess.run(["/bin/chmod", "+a", "everyone allow write", str(shared)], check=True)
            try:
                with self.assertRaisesRegex(bw.Error, "ACL"):
                    bw.homebrew()
            finally:
                subprocess.run(["/bin/chmod", "-N", str(shared)], check=True)
        # The executable itself may also pass through an unsafe symlink directory.
        (shared / "executable").symlink_to(self.brew)
        (self.home / "brew").symlink_to(shared / "executable")
        shared.chmod(0o777)
        with mock.patch.dict(os.environ, {"PATH": str(self.home)}):
            with self.assertRaisesRegex(bw.Error, "parent directory"):
                bw.homebrew()
        self.assertEqual(self.calls(), [])

    def test_brew_child_path_survives_symlink_retargeting(self):
        alias = self.home / "alias"
        alias.symlink_to(self.bin, target_is_directory=True)
        replacement = self.home / "replacement"
        replacement.mkdir()
        for directory, message in ((self.bin, "trusted"), (replacement, "redirected")):
            helper = directory / "probe"
            helper.write_text(f"#!/bin/sh\nprintf '{message}'\n")
            helper.chmod(0o700)
        with mock.patch.dict(os.environ, {"PATH": str(alias)}):
            brew, env = bw.homebrew()
        alias.unlink()
        alias.symlink_to(replacement, target_is_directory=True)
        self.assertEqual(brew, str(self.brew.resolve()))
        result = subprocess.run(["probe"], env=env, capture_output=True, text=True, timeout=5, check=True)
        self.assertEqual(result.stdout, "trusted")

    def test_brew_rejects_symlink_loops_and_resolved_path_separators(self):
        alias = self.home / "alias"
        alias.symlink_to(alias)
        with mock.patch.object(bw.shutil, "which", return_value=str(alias / "brew")):
            with self.assertRaisesRegex(bw.Error, "Too many symlinks"):
                bw.homebrew()
        alias.unlink()
        colon = self.home / "with:colon"
        colon.mkdir()
        (colon / "brew").symlink_to(self.brew)
        alias.symlink_to(colon, target_is_directory=True)
        with mock.patch.dict(os.environ, {"PATH": str(alias)}):
            with self.assertRaisesRegex(bw.Error, "PATH separator"):
                bw.homebrew()

    def test_installer_preserves_existing_acl_by_refusing_install_and_removal(self):
        rc = self.home / ".zshrc"
        for uninstall in (False, True):
            for grant in ("user:nobody deny read", "everyone allow read"):
                with self.subTest(uninstall=uninstall, grant=grant):
                    rc.write_bytes(b"# keep my settings\n")
                    rc.chmod(0o644)
                    if uninstall:
                        with contextlib.redirect_stdout(io.StringIO()):
                            installer.setup()
                    original = rc.read_bytes()
                    subprocess.run(["/bin/chmod", "+a", grant, str(rc)], check=True)
                    before = subprocess.check_output(["/bin/ls", "-le", str(rc)])
                    try:
                        with self.assertRaisesRegex(installer.Error, "existing ACL"):
                            installer.setup(uninstall=uninstall)
                        self.assertEqual(rc.read_bytes(), original)
                        self.assertEqual(subprocess.check_output(["/bin/ls", "-le", str(rc)]), before)
                        if uninstall:
                            self.assertTrue((self.home / ".local/bin/brew-watch").exists())
                    finally:
                        subprocess.run(["/bin/chmod", "-N", str(rc)], check=True)
        self.assertEqual(self.calls(), [])

    def test_guarded_write_rejects_acl_added_after_staging(self):
        rc = self.home / ".zshrc"
        rc.write_bytes(b"original")
        original_fsync = bw.os.fsync
        def add_acl(fd):
            original_fsync(fd)
            subprocess.run(["/bin/chmod", "+a", "user:nobody deny read", str(rc)], check=True)
        try:
            with bw.directory(self.home) as fd, mock.patch.object(bw.os, "fsync", side_effect=add_acl):
                with self.assertRaisesRegex(bw.Error, "existing ACL"):
                    bw.write_file(fd, ".zshrc", b"replacement", expected=b"original", reject_acl=True)
            self.assertEqual(rc.read_bytes(), b"original")
            self.assertIn(b"deny read", subprocess.check_output(["/bin/ls", "-le", str(rc)]))
            self.assertEqual(list(self.home.glob(".brew-watch-*")), [])
        finally:
            subprocess.run(["/bin/chmod", "-N", str(rc)], check=True)

    @unittest.skipUnless(sys.platform == "darwin", "requires macOS ACLs")
    def test_inherited_acl_is_rejected_before_private_data_is_written(self):
        subprocess.run(["/bin/chmod", "+a", "everyone allow read,file_inherit,directory_inherit",
                        str(self.home)], check=True)
        try:
            with bw.directory(self.home) as fd:
                with self.assertRaisesRegex(bw.Error, "ACL"):
                    bw.write_file(fd, "private-backup", b"secret")
            self.assertFalse((self.home / "private-backup").exists())
            self.assertEqual(list(self.home.glob(".brew-watch-*")), [])
            self.assertEqual(self.run_command("_startup")[0], 1)
        finally:
            subprocess.run(["/bin/chmod", "-RN", str(self.home)], check=True)

    def test_acl_read_errors_fail_closed(self):
        with bw.directory(self.home) as fd:
            bw.ctypes.set_errno(bw.errno.EACCES)
            with mock.patch.object(bw._acl, "acl_get_fd_np", return_value=None):
                with self.assertRaisesRegex(bw.Error, "ACL"):
                    bw.check_acl(fd)

    def test_oversized_normalized_scan_preserves_history_and_records_error(self):
        self.assertEqual(self.run_command("_startup")[0], 0)
        previous = self.state()
        rows = [{"name": f"p{i}", "installed_versions": ["一" * 256],
                 "current_version": "二" * 256} for i in range(400)]
        raw = json.dumps({"formulae": rows, "casks": []}, ensure_ascii=False).encode()
        self.assertLess(len(raw), bw.LIMIT)
        packages = bw.packages_from_json(raw)
        later = time.time() + bw.INTERVAL + 1
        with mock.patch.object(bw, "scan", return_value=packages), \
                mock.patch.object(bw.time, "time", return_value=later):
            result, _, error = self.run_command("_startup")
            self.assertEqual(result, 1)
            self.assertIn("size limit", error)
            state = self.state()
            self.assertEqual(state["packages"], previous["packages"])
            self.assertIn("size limit", state["error"])
            self.assertEqual(state["last_attempt"], int(later))
            self.assertEqual(state["last_check"], previous["last_check"])

    def test_automatic_report_has_bounded_rows_and_line_lengths(self):
        packages = [{"kind": "Formula", "name": f"p{i}", "installed": "1" * 26000,
                     "available": "2", "pinned": False} for i in range(5000)]
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            bw.report(packages)
        lines = output.getvalue().splitlines()
        self.assertEqual(sum(line.startswith("  Formula:") for line in lines), 20)
        self.assertLessEqual(max(map(len, lines)), 240)
        self.assertLess(len(output.getvalue()), 6000)
        self.assertIn("5000", output.getvalue())
        self.assertIn("brew outdated --verbose", output.getvalue())

    def test_private_state_and_symlink_hardlink_and_fifo_attacks(self):
        self.run_command("_startup")
        state_path = self.home / ".local/state/brew-watch/state.json"
        self.assertEqual(state_path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(state_path.parent.stat().st_mode & 0o777, 0o700)
        outside = self.home / "valuable"
        outside.write_text("keep this")
        for attack in ("symlink", "hardlink", "fifo"):
            state_path.unlink()
            if attack == "symlink":
                state_path.symlink_to(outside)
            elif attack == "hardlink":
                os.link(outside, state_path)
            else:
                os.mkfifo(state_path, 0o600)
            self.assertEqual(self.run_command("_startup")[0], 1, attack)
            self.assertEqual(outside.read_text(), "keep this")
        state_path.unlink()
        state_path.write_text("{}")
        state_path.chmod(0o644)
        self.assertEqual(self.run_command("status")[0], 1)

    def test_symlink_or_writable_parent_is_rejected(self):
        outside = self.home / "elsewhere"
        outside.mkdir()
        (self.home / ".local").symlink_to(outside)
        self.assertEqual(self.run_command("_startup")[0], 1)
        self.assertEqual(list(outside.iterdir()), [])
        (self.home / ".local").unlink()
        (self.home / ".local").mkdir(mode=0o777)
        (self.home / ".local").chmod(0o777)
        self.assertEqual(self.run_command("_startup")[0], 1)

    def test_corrupt_or_oversize_state_is_not_overwritten(self):
        self.run_command("_startup")
        state = self.home / ".local/state/brew-watch/state.json"
        for bad in (b"not json", b"{}", b"x" * (bw.LIMIT + 1)):
            state.write_bytes(bad)
            self.assertEqual(self.run_command("status")[0], 1)
            self.assertEqual(state.read_bytes(), bad)

    def test_atomic_write_failure_keeps_previous_state(self):
        self.run_command("_startup")
        path = self.home / ".local/state/brew-watch/state.json"
        original = path.read_bytes()
        with bw.directory(path.parent, private=True) as fd:
            with mock.patch.object(bw.os, "replace", side_effect=OSError("disk failure")):
                with self.assertRaises(OSError):
                    bw.write_file(fd, "state.json", b"replacement")
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(list(path.parent.glob(".brew-watch-*")), [])

    def test_cross_process_lock_prevents_duplicate_check(self):
        with bw.directory(self.home / ".local/state/brew-watch", private=True) as fd, bw.locked(fd):
            result = subprocess.run([sys.executable, "-I", str(ROOT / "brew_watch.py"), "_startup"],
                                    env=self.env, capture_output=True, text=True, timeout=5)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(self.calls(), [])

    def test_timeout_and_output_limit_terminate_worker(self):
        brew, env = bw.homebrew()
        self.config["sleep"] = 20
        self.configure()
        processes = []
        actual_popen = subprocess.Popen
        def started(*args, **kwargs):
            process = actual_popen(*args, **kwargs)
            processes.append(process)
            return process
        with mock.patch.object(bw.subprocess, "Popen", side_effect=started):
            with self.assertRaisesRegex(bw.Error, "timed out"):
                bw.capture(brew, env, ["update"], timeout=0.2)
        pid = processes[0].pid
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)
        del self.config["sleep"]
        self.config["overflow"] = True
        self.configure()
        with self.assertRaisesRegex(bw.Error, "size limit"):
            bw.capture(brew, env, ["update"], timeout=3)

    def assert_process_stopped(self, pid):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return
            time.sleep(0.02)
        self.fail(f"Process {pid} survived cleanup")

    def test_signals_cleanup_descendants_before_releasing_operation_lock(self):
        self.config["tree"] = True
        self.configure()
        for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            with self.subTest(signal=sig):
                for path in (self.bin / "pid", self.bin / "child-pid",
                             self.home / ".local/state/brew-watch/state.json"):
                    path.unlink(missing_ok=True)
                parent = subprocess.Popen([sys.executable, "-I", str(ROOT / "brew_watch.py"), "_startup"], env=self.env,
                                          stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                worker = None
                try:
                    deadline = time.monotonic() + 10
                    while not (self.bin / "child-pid").exists():
                        self.assertLess(time.monotonic(), deadline, "Fake Homebrew did not start")
                        time.sleep(0.02)
                    worker = int((self.bin / "pid").read_text())
                    child = int((self.bin / "child-pid").read_text())
                    parent.send_signal(sig)
                    time.sleep(0.1)
                    with bw.directory(self.home / ".local/state/brew-watch", private=True) as fd:
                        with self.assertRaisesRegex(bw.Error, "Another"):
                            with bw.locked(fd):
                                pass
                    parent.send_signal(sig)  # Cleanup must withstand another interrupt.
                    _, error = parent.communicate(timeout=10)
                    self.assertEqual(parent.returncode, 130, error)
                    self.assert_process_stopped(worker)
                    self.assert_process_stopped(child)
                    with bw.directory(self.home / ".local/state/brew-watch", private=True) as fd, bw.locked(fd):
                        self.assertIsNone(bw.load_state(fd)["last_check"])
                finally:
                    if parent.poll() is None:
                        parent.kill()
                    parent.communicate(timeout=5)
                    if worker is not None:
                        try:
                            os.killpg(worker, signal.SIGKILL)
                        except ProcessLookupError:
                            pass

    def test_signal_during_spawn_is_deferred_and_handlers_are_restored(self):
        brew, env = bw.homebrew()
        before = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)}
        real_popen = subprocess.Popen
        processes = []
        def spawning(*args, **kwargs):
            process = real_popen(*args, **kwargs)
            processes.append(process)
            os.kill(os.getpid(), signal.SIGTERM)
            return process
        with mock.patch.object(bw.subprocess, "Popen", side_effect=spawning):
            with self.assertRaises(KeyboardInterrupt):
                bw.capture(brew, env, ["update"])
        self.assert_process_stopped(processes[0].pid)
        self.assertEqual(before, {sig: signal.getsignal(sig) for sig in before})
        with mock.patch.object(bw.subprocess, "Popen", side_effect=OSError("cannot spawn")):
            with self.assertRaises(OSError):
                bw.capture(brew, env, ["update"])
        self.assertEqual(before, {sig: signal.getsignal(sig) for sig in before})
        bw.capture(brew, env, ["update"])
        self.assertEqual(before, {sig: signal.getsignal(sig) for sig in before})

    def test_timeout_and_output_limit_cleanup_descendants(self):
        self.config["tree"] = True
        brew, env = bw.homebrew()
        for overflow in (False, True):
            with self.subTest(overflow=overflow):
                self.config["overflow"] = overflow
                self.configure()
                for name in ("pid", "child-pid"):
                    (self.bin / name).unlink(missing_ok=True)
                try:
                    with self.assertRaisesRegex(bw.Error, "size limit" if overflow else "timed out"):
                        bw.capture(brew, env, ["update"], timeout=5 if overflow else 2)
                    for name in ("pid", "child-pid"):
                        self.assert_process_stopped(int((self.bin / name).read_text()))
                finally:
                    if (self.bin / "pid").exists():
                        try:
                            os.killpg(int((self.bin / "pid").read_text()), signal.SIGKILL)
                        except ProcessLookupError:
                            pass

    def test_root_and_elevated_execution_are_rejected(self):
        with mock.patch.object(bw.os, "getuid", return_value=0):
            self.assertEqual(self.run_command("_startup")[0], 1)
        with mock.patch.object(bw.os, "geteuid", return_value=os.getuid() + 1):
            self.assertEqual(self.run_command("_startup")[0], 1)
        self.assertEqual(self.calls(), [])

    def test_installer_is_idempotent_reversible_and_never_runs_brew(self):
        original = b"# my settings\nexport SOMETHING=unchanged"  # Preserve a missing final newline too.
        rc = self.home / ".zshrc"
        rc.write_bytes(original)
        with contextlib.redirect_stdout(io.StringIO()):
            installer.setup()
            installed = rc.read_bytes()
            installer.setup()
            self.assertEqual(rc.read_bytes(), installed)
            self.assertEqual(installed.count(installer.BEGIN), 1)
            wrapper = self.home / ".local/bin/brew-watch"
            result = subprocess.run([str(wrapper), "--help"], capture_output=True, text=True, timeout=5, env=self.env)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("status", result.stdout)
            self.assertNotIn("freshen", result.stdout)
            installer.setup(uninstall=True)
            installer.setup(uninstall=True)
        self.assertEqual(rc.read_bytes(), original)
        self.assertFalse(wrapper.exists())
        self.assertEqual(self.calls(), [])
        backups = list(self.home.glob(".zshrc.brew-watch-backup-*"))
        self.assertTrue(any(path.read_bytes() == original for path in backups))

    def test_installer_refuses_growth_beyond_read_limit_before_installing(self):
        rc = self.home / ".zshrc"
        original = b"#" + b"x" * (bw.LIMIT - 2) + b"\n"
        rc.write_bytes(original)
        with self.assertRaisesRegex(installer.Error, "exceeds the size limit"):
            installer.setup()
        self.assertEqual(rc.read_bytes(), original)
        self.assertFalse((self.home / ".local/bin/brew-watch").exists())
        self.assertFalse((self.home / ".local/share/brew-watch/startup.zsh").exists())
        self.assertEqual(list(self.home.glob(".zshrc.brew-watch-backup-*")), [])

    def test_installer_refuses_unrelated_file_or_symlink_startup(self):
        target = self.home / ".local/bin"
        target.mkdir(parents=True, mode=0o700)
        unrelated = target / "brew-watch"
        unrelated.write_text("keep this")
        with self.assertRaises(installer.Error):
            installer.setup()
        self.assertEqual(unrelated.read_text(), "keep this")
        self.assertFalse((self.home / ".zshrc").exists())
        unrelated.unlink()
        outside = self.home / "my-dotfile"
        outside.write_text("keep this too")
        (self.home / ".zshrc").symlink_to(outside)
        with self.assertRaises(OSError):
            installer.setup()
        self.assertEqual(outside.read_text(), "keep this too")

    def test_installer_refuses_malformed_markers_and_concurrent_startup_edits(self):
        rc = self.home / ".zshrc"
        for malformed in (b"\n" + installer.END + installer.BEGIN,
                          b"\n" + installer.BEGIN + installer.BEGIN + installer.END):
            rc.write_bytes(malformed)
            with self.assertRaises(installer.Error):
                installer.setup()
            self.assertEqual(rc.read_bytes(), malformed)
        rc.write_bytes(b"original\n")
        actual_read = installer.read_file
        reads = 0
        def changing(fd, name, **kwargs):
            nonlocal reads
            if name == ".zshrc":
                reads += 1
                if reads == 2:
                    rc.write_bytes(b"edited while installing\n")
            return actual_read(fd, name, **kwargs)
        with mock.patch.object(installer, "read_file", side_effect=changing):
            with self.assertRaisesRegex(installer.Error, "changed during setup"):
                installer.setup()
        self.assertEqual(rc.read_bytes(), b"edited while installing\n")

    def test_installer_preserves_edits_during_backup_on_install_and_removal(self):
        rc = self.home / ".zshrc"
        original_write = installer.write_file
        for uninstall in (False, True):
            with self.subTest(uninstall=uninstall):
                rc.write_bytes(b"original\n")
                with contextlib.redirect_stdout(io.StringIO()):
                    if uninstall:
                        installer.setup()
                    original = rc.read_bytes()
                    def writing(fd, name, data, mode=0o600, **kwargs):
                        if name.startswith(".zshrc.brew-watch-backup-"):
                            rc.write_bytes(b"late user edit\n")
                        return original_write(fd, name, data, mode, **kwargs)
                    with mock.patch.object(installer, "write_file", side_effect=writing):
                        with self.assertRaisesRegex(installer.Error, "changed during setup"):
                            installer.setup(uninstall=uninstall)
                self.assertEqual(rc.read_bytes(), b"late user edit\n")
                self.assertTrue(any(p.read_bytes() == original for p in self.home.glob(".zshrc.brew-watch-backup-*")))
                self.assertTrue((self.home / ".local/bin/brew-watch").exists())
                self.assertEqual(list(self.home.glob(".brew-watch-*")), [])

    def test_guarded_write_checks_after_staging_and_rejects_unsafe_replacements(self):
        rc = self.home / ".zshrc"
        outside = self.home / "valuable"
        outside.write_bytes(b"keep")
        original_fsync = bw.os.fsync
        for attack in ("edit", "create", "symlink", "hardlink", "permissions"):
            with self.subTest(attack=attack):
                rc.unlink(missing_ok=True)
                expected = None if attack == "create" else b"original"
                if expected is not None:
                    rc.write_bytes(expected)
                def changed(fd):
                    original_fsync(fd)
                    if attack in ("edit", "create"):
                        rc.write_bytes(b"late edit")
                    elif attack == "permissions":
                        rc.chmod(0o666)
                    else:
                        rc.unlink()
                        if attack == "symlink":
                            rc.symlink_to(outside)
                        else:
                            os.link(outside, rc)
                with bw.directory(self.home) as fd, mock.patch.object(bw.os, "fsync", side_effect=changed):
                    with self.assertRaises((bw.Error, OSError)):
                        bw.write_file(fd, ".zshrc", b"replacement", expected=expected)
                self.assertEqual(outside.read_bytes(), b"keep")
                if attack in ("edit", "create"):
                    self.assertEqual(rc.read_bytes(), b"late edit")
                self.assertEqual(list(self.home.glob(".brew-watch-*")), [])

    @unittest.skipUnless(Path("/bin/zsh").exists(), "requires the system zsh")
    def test_zsh_partial_output_does_not_block_input(self):
        producer = self.home / "notice"
        producer.write_text(f"#!{sys.executable} -I\n"
                            "import os, time\nfrom pathlib import Path\n"
                            f"home = Path({str(self.home)!r})\n"
                            "os.write(1, b'Notice: \\xe4')\n"
                            "(home / 'chunk-sent').touch()\n"
                            "while not (home / 'release-notice').exists(): time.sleep(0.02)\n"
                            "os.write(1, b'\\xb8\\x80 done\\n')\n"
                            "os.write(2, b'SECRET_DIAGNOSTIC\\x1b[2J\\n'); raise SystemExit(1)\n")
        producer.chmod(0o700)
        (self.home / ".zshrc").write_text(
            "PS1='BW_READY> '\n"
            f"typeset -g _BREW_WATCH_COMMAND={shlex.quote(str(producer))}\n"
            f"source {shlex.quote(str(ROOT / 'shell/brew-watch.zsh'))}\n")
        pid, terminal = pty.fork()
        if pid == 0:
            os.execve("/bin/zsh", ["/bin/zsh", "-d", "-i"],
                      dict(self.env, TERM="xterm-256color", ZDOTDIR=str(self.home)))
        transcript = bytearray()
        def until(condition):
            deadline = time.monotonic() + 10
            while not condition():
                self.assertLess(time.monotonic(), deadline, repr(bytes(transcript)))
                if select.select([terminal], [], [], 0.05)[0]:
                    transcript.extend(os.read(terminal, 65536))
        try:
            until(lambda: b"BW_READY> " in transcript and (self.home / "chunk-sent").exists())
            os.write(terminal, b"print BW_INPUT_OK\n")
            until(lambda: b"\r\nBW_INPUT_OK\r\n" in transcript)
            self.assertNotIn(b"Notice:", transcript)
            (self.home / "release-notice").touch()
            until(lambda: "Notice: 一 done".encode() in transcript)
            until(lambda: b"check failed; run brew-watch status" in transcript)
            self.assertNotIn(b"SECRET_DIAGNOSTIC", transcript)
            self.assertNotIn(b"\x1b[2J", transcript)
            self.assertNotIn(b"bad file descriptor", transcript)
        finally:
            (self.home / "release-notice").touch()
            os.close(terminal)
            try:
                os.kill(pid, signal.SIGHUP)
            except ProcessLookupError:
                pass
            os.waitpid(pid, 0)

    @unittest.skipUnless(Path("/bin/zsh").exists(), "requires the system zsh")
    def test_real_zsh_delivers_async_notice_preserves_input_and_reads_status(self):
        (self.home / ".zshrc").write_text("PS1='BW_READY> '\n")
        self.config["sleep"] = 1.5
        self.configure()
        with contextlib.redirect_stdout(io.StringIO()):
            installer.setup()
        env = dict(self.env, TERM="xterm-256color", ZDOTDIR=str(self.home))
        pid, terminal = pty.fork()
        if pid == 0:
            os.execve("/bin/zsh", ["/bin/zsh", "-d", "-i"], env)
        transcript = bytearray()
        def until(needle, timeout=15):
            start = len(transcript)
            deadline = time.monotonic() + timeout
            while needle not in transcript[start:]:
                if time.monotonic() >= deadline:
                    self.fail("Timed out waiting for terminal output: " + repr(bytes(transcript)))
                if select.select([terminal], [], [], 0.1)[0]:
                    try:
                        data = os.read(terminal, 65536)
                    except OSError:
                        self.fail("Terminal closed unexpectedly: " + repr(bytes(transcript)))
                    if not data:
                        self.fail("Terminal reached EOF: " + repr(bytes(transcript)))
                    transcript.extend(data)
        try:
            until(b"BW_READY> ")
            os.write(terminal, b"print -r -- BW_TYPED_OK")
            until(b"brew-watch did not request package upgrades or installations.")
            os.write(terminal, b"\n")
            until(b"\r\nBW_TYPED_OK\r\n")
            os.write(terminal, b"brew-watch status\n")
            until(b"Packages with updates at that check: 2")
            os.write(terminal, b"brew-watch freshen\n")
            until(b"invalid choice")
            self.assertEqual([call["args"] for call in self.calls()],
                             [["update"], ["outdated", "--json=v2"]])
            self.assertNotIn(b"bad file descriptor", transcript)
            self.assertEqual(len(self.state()["packages"]), 2)
        finally:
            os.close(terminal)
            try:
                os.kill(pid, signal.SIGHUP)
            except ProcessLookupError:
                pass
            os.waitpid(pid, 0)


if __name__ == "__main__":
    unittest.main()
