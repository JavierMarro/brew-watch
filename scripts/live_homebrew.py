#!/usr/bin/env python3
"""Live outdated-report smoke check on disposable GitHub-hosted macOS runners."""
import os
from pathlib import Path
import runpy
import shutil
import sys

ROOT = Path(__file__).resolve().parents[1]
bw = runpy.run_path(str(ROOT / 'brew_watch.py'))


def require_runner():
    if (sys.platform != 'darwin' or os.getuid() == 0
            or os.environ.get('GITHUB_ACTIONS') != 'true'
            or os.environ.get('RUNNER_ENVIRONMENT') != 'github-hosted'
            or os.environ.get('GITHUB_EVENT_NAME') != 'workflow_dispatch'):
        raise RuntimeError('This test requires a manually dispatched GitHub-hosted macOS runner.')


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
    packages = bw['scan'](brew, env)
    message = (f'PASS: {os.uname().machine}; brew outdated JSON parsed; '
               f'{len(packages)} outdated packages. API refresh enabled; '
               'metadata freshness is not independently verified.')
    print(message, flush=True)
    summary = os.environ.get('GITHUB_STEP_SUMMARY')
    if summary:
        with open(summary, 'a') as stream:
            stream.write(message + '\n')
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (RuntimeError, OSError, bw['Error']) as error:
        message = str(error) if isinstance(error, (RuntimeError, bw['Error'])) else type(error).__name__
        print('FAIL: ' + bw['safe_text'](message), flush=True)
        sys.exit(1)
