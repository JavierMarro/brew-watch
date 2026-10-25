"""Local tests only: no real Homebrew execution."""
import importlib.util
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('live', ROOT / 'scripts/live_homebrew.py')
live = importlib.util.module_from_spec(spec)
spec.loader.exec_module(live)


class LiveHarnessTests(unittest.TestCase):
    def test_renamed_fixture_gets_explicit_formula_trust(self):
        with mock.patch.dict(live.bw, capture=mock.Mock()):
            live.trust_renamed_fixture('/test/brew', {'HOME': '/test'})
            live.bw['capture'].assert_called_once_with(
                '/test/brew', {'HOME': '/test'},
                ['trust', '--formula', 'brewwatch/recovery-test/bw-recovery-new'])

    def test_runner_guard_fails_before_homebrew_lookup(self):
        valid = dict(GITHUB_ACTIONS='true', RUNNER_ENVIRONMENT='github-hosted',
                     GITHUB_EVENT_NAME='workflow_dispatch')
        cases = [('darwin', 501, {}), ('darwin', 0, valid), ('linux', 501, valid)]
        for key in valid:
            cases.append(('darwin', 501, dict(valid, **{key: 'wrong'})))
        for platform, uid, env in cases:
            with self.subTest(platform=platform, uid=uid, env=env), \
                    mock.patch.object(live.sys, 'platform', platform), \
                    mock.patch.object(live.os, 'getuid', return_value=uid), \
                    mock.patch.dict(os.environ, env, clear=True), \
                    mock.patch.object(live.shutil, 'which') as lookup:
                with self.assertRaisesRegex(RuntimeError, 'GitHub-hosted'):
                    live.main('signal')
                lookup.assert_not_called()

    def test_fixture_setup_answers_normal_confirmation_in_a_pty(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake = Path(tmp) / 'brew'
            fake.write_text(f'#!{sys.executable} -I\n'
                            'import sys\n'
                            'assert sys.stdin.isatty() and sys.stdout.isatty()\n'
                            'assert sys.argv[1:] == ["install", "--formula", "brewwatch/recovery-test/bw-recovery-old"]\n'
                            'print("Do you want to proceed? [y/n]", flush=True)\n'
                            'assert input() == "y"\n')
            fake.chmod(0o700)
            live.install_fixture(str(fake), dict(os.environ))
