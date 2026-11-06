"""PTY lifecycle regressions; no Homebrew or real shell configuration."""
import contextlib
import io
import json
import os
from pathlib import Path
import runpy
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]


class BenchmarkTests(unittest.TestCase):
    def test_inheritable_masters_and_shells_ignoring_hangup(self):
        with tempfile.TemporaryDirectory() as home:
            Path(home, '.zshrc').write_text("PS1='BW_READY> '\ntrap '' HUP\n")
            # Emulate older forkpty behavior even on Python versions that fixed it.
            code = '''
import errno, os, runpy, sys
b = runpy.run_path(sys.argv[1])
original = b['pty'].fork
masters = []
def inheritable_fork():
    pid, fd = original()
    if pid:
        os.set_inheritable(fd, True)
        masters.append(fd)
    return pid, fd
b['pty'].fork = inheritable_fork
waitpid = os.waitpid
def wait_after_close(pid, options):
    for fd in masters:
        try:
            os.fstat(fd)
        except OSError as error:
            assert error.errno == errno.EBADF
        else:
            raise AssertionError('PTY master still open while waiting for shell exit')
    return waitpid(pid, options)
b['os'].waitpid = wait_after_close
p, i = b['batch'](dict(os.environ), 4)
assert len(p) == len(i) == 4
'''
            result = subprocess.run([sys.executable, '-I', '-c', code,
                                     str(ROOT / 'scripts/benchmark_startup.py')],
                                    env=dict(HOME=home, ZDOTDIR=home, TERM='xterm-256color',
                                             PATH='/usr/bin:/bin', LANG='en_US.UTF-8'),
                                    capture_output=True, text=True, timeout=12)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_cached_hook_shells_exit(self):
        b = runpy.run_path(str(ROOT / 'scripts/benchmark_startup.py'))
        fixture = b['fixtures']['BrewWatchTests']()
        fixture.setUp()
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                b['fixtures']['installer'].setup()
            rc = fixture.home / '.zshrc'
            rc.write_text("PS1='BW_READY> '\n" + rc.read_text())
            bw = b['fixtures']['bw']
            with bw.directory(fixture.home / '.local/state/brew-watch', private=True) as fd:
                bw.write_file(fd, 'state.json',
                              json.dumps(dict(bw.empty_state(), last_check=int(time.time()))).encode())
            prompts, inputs = b['batch'](dict(fixture.env, TERM='xterm-256color',
                                             ZDOTDIR=str(fixture.home)), 4)
            self.assertEqual(len(prompts), 4)
            self.assertEqual(len(inputs), 4)
            self.assertEqual(fixture.calls(), [])
        finally:
            fixture.doCleanups()
