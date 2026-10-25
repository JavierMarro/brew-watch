#!/usr/bin/env python3
"""Destructive integration checks: disposable GitHub-hosted macOS runners only."""
import argparse
import hashlib
import io
import json
import os
import pty
import select
from pathlib import Path
import runpy
import shutil
import signal
import subprocess
import sys
import tarfile
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
bw = runpy.run_path(str(ROOT / 'brew_watch.py'))
TAP = 'brewwatch/recovery-test'
OLD, NEW = 'bw-recovery-old', 'bw-recovery-new'


def require_runner():
    if (sys.platform != 'darwin' or os.getuid() == 0
            or os.environ.get('GITHUB_ACTIONS') != 'true'
            or os.environ.get('RUNNER_ENVIRONMENT') != 'github-hosted'
            or os.environ.get('GITHUB_EVENT_NAME') != 'workflow_dispatch'):
        raise RuntimeError('This test requires a manually dispatched GitHub-hosted macOS runner.')


def record(message):
    print(message, flush=True)
    summary = os.environ.get('GITHUB_STEP_SUMMARY')
    if summary:
        with open(summary, 'a') as stream:
            stream.write(message + '\n\n')


def git(path, *args):
    subprocess.run(['/usr/bin/git', '-C', str(path), *args], check=True,
                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                   stderr=subprocess.DEVNULL, timeout=30)


def commit(path, message):
    git(path, 'add', '.')
    git(path, '-c', 'user.name=brew-watch test', '-c', 'user.email=test@example.invalid',
        'commit', '-m', message)


def formula(name, url, digest, checkpoint=''):
    classname = ''.join(part.capitalize() for part in name.split('-'))
    return f'''{checkpoint}
class {classname} < Formula
  desc "Disposable brew-watch migration fixture"
  homepage "https://github.com/JavierMarro/brew-watch"
  url {json.dumps(url)}
  sha256 "{digest}"
  version "1.0"
  license "MIT"
  def install
    libexec.install "payload.txt"
  end
end
'''


def install_fixture(brew, env):
    # Setup alone gets a PTY so Homebrew's normal confirmation stays enabled.
    pid, terminal = pty.fork()
    if pid == 0:
        try:
            os.execve(brew, [brew, 'install', '--formula', f'{TAP}/{OLD}'], env)
        except OSError:
            os._exit(127)
    output = bytearray()
    total = 0
    reaped = False
    try:
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            if select.select([terminal], [], [], .1)[0]:
                try:
                    chunk = os.read(terminal, 65536)
                except OSError:
                    chunk = b''
                output.extend(chunk)
                total += len(chunk)
                if total > bw['LIMIT']:
                    raise RuntimeError('Fixture install output exceeded the limit.')
                if b'[y/n]' in output:
                    os.write(terminal, b'y\n')
                    output.clear()
            done, status = os.waitpid(pid, os.WNOHANG)
            if done:
                reaped = True
                if os.waitstatus_to_exitcode(status):
                    raise RuntimeError('Fixture installation failed; no migration was tested.')
                return
        raise RuntimeError('Fixture installation timed out.')
    finally:
        os.close(terminal)
        try:
            os.killpg(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        if not reaped:
            os.waitpid(pid, 0)


def main(mode):
    require_runner()  # Must precede all writes and Homebrew execution.
    if mode == 'worker':
        return bw['main'](['_startup'])

    # Hosted images can have admin-group writable Homebrew directories. Tighten
    # only runner-owned paths; never relax validation or use sudo.
    found = shutil.which('brew')
    if not found:
        raise RuntimeError('Homebrew is missing from the runner image.')
    for path in set(Path(found).absolute().parents) | set(Path(found).resolve().parents):
        info = path.stat()
        if info.st_uid == os.getuid() and info.st_mode & 0o022:
            path.chmod(info.st_mode & 0o755)
    brew, env = bw['homebrew']()
    record('Runner: ' + os.uname().machine + '; scenario: ' + mode)
    record('Homebrew: ' + bw['safe_text'](bw['capture'](brew, env, ['--version']).decode()))
    bw['scan'](brew, env)
    record('PASS: real brew update and outdated JSON parsing.')
    # Refresh can replace Homebrew; validate again before fixture setup.
    brew, env = bw['homebrew']()
    prefix = Path(bw['capture'](brew, env, ['--prefix']).decode().strip())
    repository = Path(bw['capture'](brew, env, ['--repository']).decode().strip())
    record('Homebrew revision: ' + subprocess.check_output(
        ['/usr/bin/git', '-C', str(repository), 'rev-parse', 'HEAD'], text=True, timeout=30).strip())
    state_path = Path.home() / '.local/state/brew-watch'
    with tempfile.TemporaryDirectory(prefix='bw-live-', dir=Path.home()) as temporary:
        base = Path(temporary)
        origin = base / 'origin'
        origin.mkdir()
        (origin / 'Formula').mkdir()
        archive = base / 'fixture.tar.gz'
        payload = b'brew-watch migration fixture\n'
        with tarfile.open(archive, 'w:gz') as tar:
            entry = tarfile.TarInfo('fixture/payload.txt')
            entry.size = len(payload)
            tar.addfile(entry, io.BytesIO(payload))
        digest = hashlib.sha256(archive.read_bytes()).hexdigest()
        (origin / 'Formula' / f'{OLD}.rb').write_text(formula(OLD, archive.as_uri(), digest))
        git(origin, 'init', '-b', 'main')
        commit(origin, 'Initial fixture')
        bw['capture'](brew, env, ['tap', TAP, str(origin)])
        install_fixture(brew, env)
        old_payload = prefix / 'Cellar' / OLD / '1.0/libexec/payload.txt'
        if old_payload.read_bytes() != payload:
            raise RuntimeError('Fixture install did not produce the expected payload.')
        armed, marker = base / 'armed', base / 'checkpoint'
        checkpoint = f'''
require "migrator"
module BrewWatchCheckpoint
  def move_to_new_directory
    super
    if oldname == "{OLD}" && File.exist?({json.dumps(str(armed))})
      File.write({json.dumps(str(marker) + ".tmp")}, "#{{Process.pid}} #{{Process.getpgrp}}")
      File.rename({json.dumps(str(marker) + ".tmp")}, {json.dumps(str(marker))})
      sleep 300
    end
  end
end
Migrator.prepend(BrewWatchCheckpoint)
'''
        (origin / 'Formula' / f'{OLD}.rb').unlink()
        (origin / 'Formula' / f'{NEW}.rb').write_text(formula(NEW, archive.as_uri(), digest, checkpoint))
        (origin / 'formula_renames.json').write_text(json.dumps({OLD: NEW}))
        commit(origin, 'Rename fixture and pause after the real Cellar move')
        armed.touch()
        baseline = dict(bw['empty_state'](), last_check=int(time.time()) - bw['INTERVAL'] - 1)
        with bw['directory'](state_path, private=True) as fd:
            bw['save_state'](fd, baseline)
        worker_env = dict(env, **{k: os.environ[k] for k in
            ('GITHUB_ACTIONS', 'RUNNER_ENVIRONMENT', 'GITHUB_EVENT_NAME')})
        worker = subprocess.Popen([sys.executable, '-I', str(Path(__file__).resolve()), 'worker'],
            env=worker_env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, start_new_session=True)
        group = None
        try:
            deadline = time.monotonic() + 110
            while not marker.exists():
                if worker.poll() is not None or time.monotonic() >= deadline:
                    raise RuntimeError('Migration checkpoint was not reached; this is NOT a passing interruption test.')
                time.sleep(.1)
            _, group = map(int, marker.read_text().split())
            new_payload = prefix / 'Cellar' / NEW / '1.0/libexec/payload.txt'
            if new_payload.read_bytes() != payload:
                raise RuntimeError('Checkpoint did not follow a real Cellar mutation.')
            record('PASS: real formula rename reached the checkpoint after moving installed files.')
            if mode == 'signal':
                worker.send_signal(signal.SIGTERM)
            result = worker.wait(timeout=135)
            if result != (130 if mode == 'signal' else 1):
                raise RuntimeError(f'Unexpected interrupted worker exit: {result}')
            deadline = time.monotonic() + 10
            while True:
                try:
                    os.killpg(group, 0)
                except ProcessLookupError:
                    break
                if time.monotonic() >= deadline:
                    raise RuntimeError('Captured Homebrew process group survived cleanup.')
                time.sleep(.1)
            with bw['directory'](state_path, private=True) as fd, bw['locked'](fd):
                saved = bw['load_state'](fd)
                if saved['last_check'] != baseline['last_check'] or saved['packages'] != baseline['packages']:
                    raise RuntimeError('Interrupted check replaced successful history.')
                if mode == 'timeout' and 'timed out' not in (saved['error'] or ''):
                    raise RuntimeError('The production timeout was not exercised.')
            record('PASS: worker failed as expected, process group stopped, lock released, history preserved.')
            armed.unlink()
            # Retry eligibility is advanced only in the disposable test state.
            with bw['directory'](state_path, private=True) as fd:
                saved['last_attempt'] = int(time.time()) - bw['RETRY'] - 1
                bw['save_state'](fd, saved)
            if bw['main'](['_startup']) != 0:
                raise RuntimeError('Automatic retry failed after interruption.')
            if new_payload.exists() and new_payload.read_bytes() == payload:
                receipt = json.loads((new_payload.parents[1] / 'INSTALL_RECEIPT.json').read_text())
                if Path(receipt['source']['path']).name == f'{NEW}.rb':
                    record('PASS: automatic retry recovered the renamed payload and receipt.')
                    return 0
            record('FAIL: automatic retry did not complete migration; checking explicit brew migrate repair.')
            bw['capture'](brew, env, ['migrate', f'{TAP}/{OLD}'])
            if new_payload.read_bytes() == payload:
                record('Explicit brew migrate restored the fixture payload; automatic recovery still FAILED.')
            return 1
        finally:
            armed.unlink(missing_ok=True)
            if worker.poll() is None:
                worker.terminate()
                try:
                    worker.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    bw['stop'](worker)
            if group is not None:
                try:
                    os.killpg(group, signal.SIGKILL)
                except ProcessLookupError:
                    pass
    return 0


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=('signal', 'timeout', 'worker'))
    args = parser.parse_args()
    try:
        sys.exit(main(args.mode))
    except (RuntimeError, OSError, bw['Error'], subprocess.SubprocessError) as error:
        # No raw subprocess diagnostics or environment values in CI summaries.
        message = str(error) if isinstance(error, (RuntimeError, bw['Error'])) else type(error).__name__
        print('FAIL: ' + bw['safe_text'](message), flush=True)
        sys.exit(1)
