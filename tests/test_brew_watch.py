import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import pty
import select
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
import json, os, subprocess, sys, time
from pathlib import Path
base = Path(__file__).parent
config = json.loads((base / "config.json").read_text())
args = sys.argv[1:]
with (base / "calls.jsonl").open("a") as log:
    log.write(json.dumps({"args": args, "env": dict(os.environ)}) + "\n")
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
if args[0] == "upgrade":
    config["reply"] = {"formulae": [], "casks": []}
    (base / "config.json").write_text(json.dumps(config))
'''


class TTY(io.StringIO):
    def isatty(self):
        return True


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

    def run_command(self, command, interactive=False):
        output, error = TTY() if interactive else io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(error), mock.patch.object(sys, "stdin", TTY() if interactive else io.StringIO()):
            result = bw.main([command])
        return result, output.getvalue(), error.getvalue()

    def state(self):
        return json.loads((self.home / ".local/state/brew-watch/state.json").read_text())

    def test_startup_checks_once_and_covers_formulae_and_casks(self):
        result, output, error = self.run_command("_startup")
        self.assertEqual(result, 0, error)
        self.assertIn("Formula: example-tool", output)
        self.assertIn("Cask: example-app", output)
        self.assertIn("No packages have been upgraded", output)
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

    def test_freshen_requires_terminal_before_any_homebrew_command(self):
        result, _, error = self.run_command("freshen")
        self.assertEqual(result, 1)
        self.assertIn("interactive", error)
        self.assertEqual(self.calls(), [])

    def test_explicit_freshen_sequence_and_post_upgrade_status(self):
        result, output, error = self.run_command("freshen", interactive=True)
        self.assertEqual(result, 0, error)
        self.assertEqual([call["args"] for call in self.calls()],
                         [["update"], ["outdated", "--json=v2"], ["upgrade", "--ask"], ["outdated", "--json=v2"]])
        self.assertEqual(self.state()["packages"], [])
        self.assertIn("Freshening complete", output)

    def test_failed_refresh_or_invalid_json_cannot_reach_upgrade(self):
        self.config["fail"] = "update"
        self.configure()
        self.assertEqual(self.run_command("freshen", interactive=True)[0], 1)
        self.assertEqual([call["args"] for call in self.calls()], [["update"]])
        del self.config["fail"]
        self.config["reply"] = {"formulae": "invalid", "casks": []}
        self.configure()
        self.assertEqual(self.run_command("freshen", interactive=True)[0], 1)
        self.assertFalse(any(call["args"][0] == "upgrade" for call in self.calls()))

    def test_upgrade_failure_is_reported_and_remaining_packages_are_retained(self):
        self.config["fail"] = "upgrade"
        self.configure()
        result, _, error = self.run_command("freshen", interactive=True)
        self.assertEqual(result, 1)
        self.assertIn("some packages may have changed", error)
        self.assertEqual(len(self.state()["packages"]), 2)
        self.assertNotIn("SECRET_TOKEN", self.state()["error"])

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
        self.assertEqual(self.run_command("freshen", interactive=True)[0], 1)
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
            self.assertIn("freshen", result.stdout)
            installer.setup(uninstall=True)
            installer.setup(uninstall=True)
        self.assertEqual(rc.read_bytes(), original)
        self.assertFalse(wrapper.exists())
        self.assertEqual(self.calls(), [])
        backups = list(self.home.glob(".zshrc.brew-watch-backup-*"))
        self.assertTrue(any(path.read_bytes() == original for path in backups))

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

    @unittest.skipUnless(Path("/bin/zsh").exists(), "requires the system zsh")
    def test_real_zsh_delivers_async_notice_preserves_input_and_runs_freshen(self):
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
            until(b"No packages have been upgraded.")
            os.write(terminal, b"\n")
            until(b"\r\nBW_TYPED_OK\r\n")
            os.write(terminal, b"brew-watch freshen\n")
            until(b"Freshening complete", timeout=20)
            self.assertEqual(sum(call["args"][0] == "upgrade" for call in self.calls()), 1)
            self.assertNotIn(b"bad file descriptor", transcript)
            self.assertEqual(self.state()["packages"], [])
        finally:
            os.close(terminal)
            try:
                os.kill(pid, signal.SIGHUP)
            except ProcessLookupError:
                pass
            os.waitpid(pid, 0)


if __name__ == "__main__":
    unittest.main()
