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
    def test_recovery_requires_intact_payload_and_renamed_receipt(self):
        with tempfile.TemporaryDirectory() as tmp:
            keg = Path(tmp) / '1.0'
            path = keg / 'libexec/payload.txt'
            path.parent.mkdir(parents=True)
            path.write_bytes(b'fixture')
            receipt = keg / 'INSTALL_RECEIPT.json'
            for content in ('{}', 'invalid', '{"source":{"path":"bw-recovery-old.rb"}}'):
                receipt.write_text(content)
                self.assertFalse(live.fixture_recovered(path, b'fixture'))
            receipt.write_text('{"source":{"path":"bw-recovery-new.rb"}}')
            self.assertTrue(live.fixture_recovered(path, b'fixture'))
            self.assertFalse(live.fixture_recovered(path, b'other'))

    def test_failed_retry_attempts_manual_repair_but_never_passes(self):
        for repaired in (False, True):
            with self.subTest(repaired=repaired), \
                    mock.patch.dict(live.bw, main=mock.Mock(return_value=1),
                                    capture=mock.Mock(), scan=mock.Mock()), \
                    mock.patch.object(live, 'record'), \
                    mock.patch.object(live, 'fixture_recovered', return_value=repaired):
                path = mock.Mock()
                self.assertEqual(live.check_recovery('/test/brew', {}, path, path, b'fixture'), 1)
                live.bw['capture'].assert_called_once_with(
                    '/test/brew', {}, ['migrate', 'brewwatch/recovery-test/bw-recovery-old'])
                self.assertEqual(live.bw['scan'].call_count, int(repaired))

    def test_successful_recovery_does_not_attempt_manual_repair(self):
        with mock.patch.dict(live.bw, main=mock.Mock(return_value=0), capture=mock.Mock()), \
                mock.patch.object(live, 'record'), \
                mock.patch.object(live, 'fixture_recovered', return_value=True):
            path = mock.Mock()
            self.assertEqual(live.check_recovery('/test/brew', {}, path, path, b'fixture'), 0)
            live.bw['capture'].assert_not_called()

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
