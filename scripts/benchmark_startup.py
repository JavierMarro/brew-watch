#!/usr/bin/env python3
"""Repeatable PTY timings; temporary homes and fake Homebrew only."""
import contextlib
import io
import json
import os
from pathlib import Path
import pty
import runpy
import select
import signal
import statistics
import time

ROOT = Path(__file__).resolve().parents[1]
fixtures = runpy.run_path(str(ROOT / 'tests/test_brew_watch.py'))


def batch(env, count):
    sessions = {}
    prompt_times, input_times = [], []
    try:
        for _ in range(count):
            start = time.perf_counter()
            pid, fd = pty.fork()
            if pid == 0:
                os.execve('/bin/zsh', ['/bin/zsh', '-d', '-i'], env)
            sessions[fd] = [pid, start, None, bytearray(), False]
        deadline = time.monotonic() + 15
        while not all(s[4] for s in sessions.values()):
            if time.monotonic() > deadline:
                raise RuntimeError('PTY benchmark timed out')
            readable, _, _ = select.select([fd for fd, s in sessions.items() if not s[4]], [], [], .1)
            for fd in readable:
                s = sessions[fd]
                chunk = os.read(fd, 65536)
                if not chunk:
                    raise RuntimeError('Shell closed before responding')
                s[3].extend(chunk)
                now = time.perf_counter()
                if s[2] is None and b'BW_READY> ' in s[3]:
                    prompt_times.append((now - s[1]) * 1000)
                    s[3].clear()
                    s[2] = time.perf_counter()
                    os.write(fd, b'print BW_INPUT_OK\n')
                elif s[2] is not None and b'\r\nBW_INPUT_OK\r\n' in s[3]:
                    input_times.append((now - s[2]) * 1000)
                    s[4] = True
        # Let fresh-state workers finish before removing their temporary home.
        time.sleep(.3)
        return prompt_times, input_times
    finally:
        for fd, (pid, *_rest) in sessions.items():
            os.close(fd)
            try:
                os.kill(pid, signal.SIGHUP)
            except ProcessLookupError:
                pass
            os.waitpid(pid, 0)


def main():
    fixture = fixtures['BrewWatchTests']()
    fixture.setUp()
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            fixtures['installer'].setup()
        rc = fixture.home / '.zshrc'
        hook = rc.read_text()
        state_dir = fixture.home / '.local/state/brew-watch'
        bw = fixtures['bw']
        env = dict(fixture.env, TERM='xterm-256color', ZDOTDIR=str(fixture.home))
        for large in (False, True):
            state = dict(bw.empty_state(), last_check=int(time.time()))
            if large:
                row = dict(kind='Formula', name='example', installed='1' * 25000,
                           available='2', pinned=False)
                state['packages'] = [row] * 40
            with bw.directory(state_dir, private=True) as fd:
                bw.save_state(fd, state)
            for count in (1, 4):
                for enabled in (False, True):
                    rc.write_text("PS1='BW_READY> '\n" + (hook if enabled else ''))
                    prompts, inputs = [], []
                    for _ in range(10):
                        p, i = batch(env, count)
                        prompts.extend(p)
                        inputs.extend(i)
                    print(json.dumps(dict(hook=enabled, state_bytes=len(json.dumps(state)),
                          simultaneous=count, samples=len(prompts),
                          prompt_median_ms=round(statistics.median(prompts), 2),
                          prompt_max_ms=round(max(prompts), 2),
                          input_median_ms=round(statistics.median(inputs), 2),
                          input_max_ms=round(max(inputs), 2))), flush=True)
        assert fixture.calls() == [], 'Fresh state must never contact Homebrew'
    finally:
        fixture.doCleanups()


if __name__ == '__main__':
    main()
