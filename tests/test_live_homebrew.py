"""Local tests only: no real Homebrew execution."""
import importlib.util
import os
from pathlib import Path
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('live', ROOT / 'scripts/live_homebrew.py')
live = importlib.util.module_from_spec(spec)
spec.loader.exec_module(live)


class LiveHarnessTests(unittest.TestCase):
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
                    live.main()
                lookup.assert_not_called()
