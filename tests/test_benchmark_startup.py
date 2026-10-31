"""PTY lifecycle regressions; no Homebrew or real shell configuration."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class BenchmarkTests(unittest.TestCase):
    def test_inheritable_masters_and_shells_ignoring_hangup(self):
        with tempfile.TemporaryDirectory() as home:
            Path(home, '.zshrc').write_text("PS1='BW_READY> '\ntrap '' HUP\n")
            # Emulate older forkpty behavior even on Python versions that fixed it.
            code = '''
import os, runpy, sys
b = runpy.run_path(sys.argv[1])
original = b['pty'].fork
def inheritable_fork():
    pid, fd = original()
    if pid:
        os.set_inheritable(fd, True)
    return pid, fd
b['pty'].fork = inheritable_fork
p, i = b['batch'](dict(os.environ), 4)
assert len(p) == len(i) == 4
'''
            result = subprocess.run([sys.executable, '-I', '-c', code,
                                     str(ROOT / 'scripts/benchmark_startup.py')],
                                    env=dict(HOME=home, ZDOTDIR=home, TERM='xterm-256color',
                                             PATH='/usr/bin:/bin', LANG='en_US.UTF-8'),
                                    capture_output=True, text=True, timeout=12)
            self.assertEqual(result.returncode, 0, result.stderr)
