#!/usr/bin/env python3
"""Live outdated-report smoke check on disposable GitHub-hosted macOS runners."""
import contextlib
import hashlib
from http.server import BaseHTTPRequestHandler, HTTPServer
import os
import platform
from pathlib import Path
import runpy
import shutil
import sys
import tempfile
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
bw = runpy.run_path(str(ROOT / 'brew_watch.py'))


def require_runner():
    if (sys.platform != 'darwin' or os.getuid() == 0
            or os.environ.get('GITHUB_ACTIONS') != 'true'
            or os.environ.get('RUNNER_ENVIRONMENT') != 'github-hosted'
            or os.environ.get('GITHUB_EVENT_NAME') != 'workflow_dispatch'):
        raise RuntimeError('This test requires a manually dispatched GitHub-hosted macOS runner.')


def record(message):
    message = bw['safe_text'](message)
    print(message, flush=True)
    summary = os.environ.get('GITHUB_STEP_SUMMARY')
    if summary:
        with open(summary, 'a') as stream:
            stream.write(message + '\n\n')


def catalogs(cache):
    result = {}
    for path in (cache / 'api').rglob('*.jws.json'):
        with path.open('rb') as stream:
            digest = hashlib.file_digest(stream, 'sha256').hexdigest()
        result[path] = (path.stat().st_mtime_ns, path.stat().st_size, digest)
    return result


def age_catalogs(cache):
    old = time.time() - 30 * 24 * 60 * 60
    files = catalogs(cache)
    if not files or any(size == 0 for _, size, _ in files.values()):
        raise RuntimeError('No populated signed API catalog found; freshness was NOT verified.')
    for path in files:
        os.utime(path, (old, old))
    return catalogs(cache)


@contextlib.contextmanager
def blocked_refresh(env):
    class RejectProxy(BaseHTTPRequestHandler):
        attempts = 0

        def do_CONNECT(self):
            type(self).attempts += 1
            self.send_error(502)

        do_GET = do_CONNECT

        def log_message(self, *args):
            pass  # Never log request URLs, headers, or proxy credentials.

    with HTTPServer(('127.0.0.1', 0), RejectProxy) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        proxy = f'http://127.0.0.1:{server.server_port}'
        blocked = dict(env)
        for key in ('HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'http_proxy', 'https_proxy', 'all_proxy'):
            blocked[key] = proxy
        blocked.update(NO_PROXY='', no_proxy='')
        try:
            yield blocked, RejectProxy
        finally:
            server.shutdown()
            thread.join(timeout=5)


def verify_cache(brew, env, cache):
    # Cache override is test-only; production still uses Homebrew's normal cache.
    env = dict(env, HOMEBREW_CACHE=str(cache))
    packages = bw['scan'](brew, env)
    before = age_catalogs(cache)
    record(f'PASS: empty-cache report parsed; {len(before)} signed catalog(s) downloaded; '
           f'{len(packages)} outdated packages.')
    bw['scan'](brew, env)
    after = catalogs(cache)
    if any(path not in after or after[path][0] <= values[0] for path, values in before.items()):
        raise RuntimeError('Stale catalog was not revalidated; freshness was NOT verified.')
    record('PASS: stale API catalogs revalidated (replacement or conditional response).')
    before = age_catalogs(cache)
    with blocked_refresh(env) as (blocked, proxy):
        bw['scan'](brew, blocked)
        if proxy.attempts == 0 or catalogs(cache) != before:
            raise RuntimeError('Cached fallback was not demonstrated with a rejected refresh.')
        record('PASS: refresh requests rejected; valid cached report returned; catalog unchanged.')
        attempts = proxy.attempts
        try:
            bw['scan'](brew, dict(blocked, HOMEBREW_CACHE=str(cache / 'empty-control')))
        except bw['Error']:
            if proxy.attempts == attempts:
                raise RuntimeError('Empty-cache control failed before reaching the rejecting proxy.') from None
        else:
            raise RuntimeError('Empty-cache blocked-refresh control unexpectedly succeeded.')
        record('PASS: rejected refresh with no cached catalog fails instead of reporting success.')


def main():
    require_runner()  # Must precede all writes and Homebrew execution.
    found = shutil.which('brew')
    if not found:
        raise RuntimeError('Homebrew is missing from the runner image.')
    # Hosted images may have writable parents; tighten runner-owned paths only.
    for path in set(Path(found).absolute().parents) | set(Path(found).resolve().parents):
        info = path.stat()
        if info.st_uid == os.getuid() and info.st_mode & 0o022:
            path.chmod(info.st_mode & 0o755)
    brew, env = bw['homebrew']()
    versions = bw['capture'](brew, env, ['--version']).decode('utf-8', errors='replace').splitlines()
    if not versions:
        raise RuntimeError('Homebrew returned no version information.')
    version = versions[0]
    record(f'Runner: {platform.machine()}; macOS {platform.mac_ver()[0]}; '
           f'Python {platform.python_version()}; {version}; commit {os.environ.get("GITHUB_SHA", "unknown")}')
    with tempfile.TemporaryDirectory(prefix='bw-api-check-') as temporary:
        verify_cache(brew, env, Path(temporary))
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (RuntimeError, OSError, bw['Error']) as error:
        message = str(error) if isinstance(error, (RuntimeError, bw['Error'])) else type(error).__name__
        print('FAIL: ' + bw['safe_text'](message), flush=True)
        sys.exit(1)
