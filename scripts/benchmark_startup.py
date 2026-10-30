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
# A coarse CI regression guard, not a promise about interactive latency.
LATENCY_LIMIT_MS = 1000


def batch(env, count, *, active=False):
    sessions = {}
    prompt_times, input_times = [], []
    progress_seen = notice_seen = False
    try:
        for _ in range(count):
            start = time.perf_counter()
            pid, fd = pty.fork()
            if pid == 0:
                os.execve('/bin/zsh', ['/bin/zsh', '-d', '-i'], env)
            sessions[fd] = dict(pid=pid, start=start, sent=None, buffer=bytearray(),
                                sequence=0, after_notice=0, ready_at=None, done=False)
        deadline = time.monotonic() + 15
        while not all(s['done'] for s in sessions.values()):
            if time.monotonic() > deadline:
                raise RuntimeError('PTY benchmark timed out')
            for fd, s in sessions.items():
                if s['ready_at'] is not None and time.perf_counter() >= s['ready_at']:
                    s['sent'] = time.perf_counter()
                    # The response must differ from echoed input, even amid ZLE redraws.
                    os.write(fd, f"print BW_INPUT_$(({s['sequence']}))\n".encode())
                    s['ready_at'] = None
            readable, _, _ = select.select([fd for fd, s in sessions.items() if not s['done']], [], [], .01)
            for fd in readable:
                s = sessions[fd]
                chunk = os.read(fd, 65536)
                if not chunk:
                    raise RuntimeError('Shell closed before responding')
                s['buffer'].extend(chunk)
                progress_seen |= b'Automatically checking your formulae and casks' in s['buffer']
                notice_seen |= b'brew update && brew outdated --verbose' in s['buffer']
                now = time.perf_counter()
                if s['sent'] is None and b'BW_READY> ' in s['buffer']:
                    prompt_times.append((now - s['start']) * 1000)
                    s['sent'] = now
                elif s['sent'] is not None:
                    response = f"BW_INPUT_{s['sequence']}\r\n".encode()
                    if response not in s['buffer']:
                        continue
                    input_times.append((now - s['sent']) * 1000)
                    if notice_seen:
                        s['after_notice'] += 1
                    if not active or s['after_notice'] >= 3:
                        s['done'] = True
                        continue
                    s['sequence'] += 1
                else:
                    continue
                s['buffer'].clear()
                # Leave ZLE time for worker callbacks without delaying other terminals.
                s['ready_at'] = time.perf_counter() + .02
        if active and not (progress_seen and notice_seen):
            raise RuntimeError('Active benchmark did not observe progress and reminder delivery')
        time.sleep(.3)  # Let cached-state workers close before removing their home.
        return prompt_times, input_times
    finally:
        for fd, s in sessions.items():
            os.close(fd)
            try:
                os.kill(s['pid'], signal.SIGHUP)
            except ProcessLookupError:
                pass
            os.waitpid(s['pid'], 0)


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
        fixture.config.update(sleep=1.2, reply={'formulae': [dict(name=f'example-{i}',
            installed_versions=['一' * 128], current_version='二' * 128) for i in range(400)], 'casks': []})
        fixture.configure()
        for scenario in ('cached', 'legacy', 'active'):
            for count in (1, 4):
                for enabled in (False, True):
                    rc.write_text("PS1='BW_READY> '\n" + (hook if enabled else ''))
                    prompts, inputs = [], []
                    calls_before = len(fixture.calls())
                    batches = 3 if scenario == 'active' else 10
                    for _ in range(batches):
                        state = dict(bw.empty_state(), last_check=int(time.time()))
                        if scenario == 'legacy':
                            del state['package_count']
                            state['packages'] = [dict(kind='Formula', name='example', installed='1' * 25000,
                                                      available='2', pinned=False)] * 40
                        elif scenario == 'active':
                            state = bw.empty_state()
                        raw = json.dumps(state).encode()
                        with bw.directory(state_dir, private=True) as fd:
                            bw.write_file(fd, 'state.json', raw)
                        p, i = batch(env, count, active=scenario == 'active' and enabled)
                        prompts.extend(p)
                        inputs.extend(i)
                    expected = batches if scenario == 'active' and enabled else 0
                    if len(fixture.calls()) - calls_before != expected:
                        raise RuntimeError('Unexpected scan count across concurrent terminals')
                    print(json.dumps(dict(scenario=scenario, hook=enabled, state_bytes=len(raw),
                          simultaneous=count, prompt_samples=len(prompts), input_samples=len(inputs),
                          prompt_median_ms=round(statistics.median(prompts), 2),
                          prompt_max_ms=round(max(prompts), 2),
                          input_median_ms=round(statistics.median(inputs), 2),
                          input_max_ms=round(max(inputs), 2))), flush=True)
                    if max(prompts + inputs) > LATENCY_LIMIT_MS:
                        raise RuntimeError(f'PTY latency exceeded the {LATENCY_LIMIT_MS} ms regression limit')
    finally:
        fixture.doCleanups()


if __name__ == '__main__':
    main()
