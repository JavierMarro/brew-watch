"""Local tests only: no real Homebrew execution."""
import importlib.util
from http.client import HTTPConnection
from urllib.parse import urlsplit
import os
import tempfile
from types import SimpleNamespace
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

    def test_rejecting_proxy_counts_requests_without_forwarding(self):
        with live.blocked_refresh({'NO_PROXY': '*', 'https_proxy': 'old'}) as (env, proxy):
            address = urlsplit(env['HTTPS_PROXY'])
            self.assertEqual(address.hostname, '127.0.0.1')
            self.assertEqual(env['NO_PROXY'], '')
            self.assertEqual(env['https_proxy'], env['HTTPS_PROXY'])
            connection = HTTPConnection(address.hostname, address.port, timeout=2)
            try:
                connection.request('CONNECT', 'example.invalid:443')
                response = connection.getresponse()
                self.assertEqual(response.status, 502)
                response.read()
                self.assertEqual(proxy.attempts, 1)
            finally:
                connection.close()

    def test_cache_verification_rejects_missing_refresh_and_false_fallback(self):
        errors = {'no_catalog': 'No populated', 'stale': 'not revalidated',
                  'no_proxy': 'no requests', 'content': 'changed catalog',
                  'removed': 'changed catalog',
                  'added': 'changed catalog', 'empty_succeeds': 'unexpectedly succeeded',
                  'empty_no_proxy': 'before reaching'}
        for fault in (None, 'timestamp', *errors):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as tmp:
                cache = Path(tmp)
                catalog = cache / 'api/internal/packages.test.jws.json'
                proxy = SimpleNamespace(attempts=0)
                calls = []

                def scan(brew, env):
                    calls.append(env.copy())
                    stage = len(calls)
                    if stage == 1 and fault != 'no_catalog':
                        catalog.parent.mkdir(parents=True)
                        catalog.write_text('signed fixture placeholder')
                    elif stage == 2 and fault != 'stale':
                        os.utime(catalog, None)
                    elif stage == 3 and fault != 'no_proxy':
                        proxy.attempts += 1
                        if fault == 'content':
                            catalog.write_text('SIGNED fixture placeholder')
                        elif fault == 'timestamp':
                            os.utime(catalog, None)
                        elif fault == 'removed':
                            catalog.unlink()
                        elif fault == 'added':
                            catalog.with_name('extra.jws.json').write_text('extra')
                    elif stage == 4:
                        if fault != 'empty_no_proxy':
                            proxy.attempts += 1
                        if fault != 'empty_succeeds':
                            raise live.bw['Error']('Blocked refresh')
                    return []

                with mock.patch.dict(live.bw, scan=scan), \
                        mock.patch.object(live, 'record') as record, \
                        mock.patch.object(live, 'blocked_refresh') as blocked:
                    blocked.return_value.__enter__.return_value = (
                        {'HOMEBREW_CACHE': str(cache)}, proxy)
                    if fault in errors:
                        with self.assertRaisesRegex(RuntimeError, errors[fault]):
                            live.verify_cache('/test/brew', {}, cache)
                    else:
                        live.verify_cache('/test/brew', {}, cache)
                        self.assertEqual(len(calls), 4)
                        self.assertEqual(calls[-1]['HOMEBREW_CACHE'], str(cache / 'empty-control'))
                        self.assertTrue(any('catalog contents unchanged' in call.args[0]
                                            for call in record.call_args_list))
                    if fault not in ('no_catalog', 'stale'):
                        self.assertEqual(len(calls), 4)
                        evidence = next(call.args[0] for call in record.call_args_list
                                        if call.args[0].startswith('Fallback evidence:'))
                        self.assertIn('rejected requests=' + ('0' if fault == 'no_proxy' else '1'), evidence)
                        expected = {'content': 'content changed=1', 'timestamp': 'timestamps changed=1',
                                    'removed': 'removed=1', 'added': 'added=1'}
                        if fault in expected:
                            self.assertIn(expected[fault], evidence)
