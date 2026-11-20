#!/usr/bin/env python3
"""Isolated fake DSH transport shared by parent and detached review processes.

This is a synthetic native protocol/persistence fixture, never an installed DSH
qualification. All records live under a marked temporary test directory.
"""
import fcntl
import json
import os
from pathlib import Path
import sys
import time
import uuid


def atomic(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex)
    temporary.write_text(json.dumps(value, ensure_ascii=False))
    os.replace(temporary, path)


def record_session(home, native, terminal, text, route):
    home = Path(home)
    if not (home / 'ISOLATED-COLLABORATION-FIXTURE').is_file():
        raise ValueError('Native protocol fixture must use its marked temporary home')
    directory = home / 'sessions' / native['id']
    directory.mkdir(parents=True, exist_ok=True)
    (directory / 'session.lock').touch(exist_ok=True)
    path = directory / 'fixture-native.json'
    value = {'source': 'dsh_persisted_session_log', 'fixture_transport': True,
             'session_id': native['id'], 'cwd': native['cwd'],
             'latest_turn': int(native['turn_id']), 'terminal': terminal,
             'latest_terminal': terminal, 'final_text': text,
             'artifact_path': str(path), 'route': route}
    atomic(path, value)
    return value


def read_session(home, native):
    path = Path(home) / 'sessions' / native['id'] / 'fixture-native.json'
    try:
        value = json.loads(path.read_bytes())
    except (OSError, ValueError):
        return {'read_observation_issue': 'Exact isolated fixture Session is unavailable'}
    if value['session_id'] != native['id']:
        raise ValueError('Fixture query changed the original Session')
    return value


def main(argv):
    if '--query' in argv:
        index = argv.index('--query')
        sessions, session_id, turn = argv[index + 2:index + 5]
        print(json.dumps(read_session(Path(sessions).parent, {'id': session_id, 'turn_id': turn})))
        return 0
    state = json.loads(Path(os.environ['HTAM_COLLAB_FIXTURE_STATE']).read_bytes())
    assert Path.cwd() == Path(state['root'])
    assert not any(os.environ.get(key) for key in ('CODEX_THREAD_ID', 'DSH_SESSION_ID', 'DSH_SHELL'))
    patch = json.loads(Path(argv[argv.index('--patch') + 1]).read_bytes())
    tools = patch[-1]['insert'][0]['config']['allow']
    assert not {'write', 'edit', 'request_review'}.intersection(tools)
    route = patch[0]['config']
    native = {'id': 'fixture-review-' + uuid.uuid4().hex, 'turn_id': '1', 'cwd': str(Path.cwd())}
    record_session(state['home'], native, None, None, route)
    lock = Path(state['home']) / 'sessions' / native['id'] / 'session.lock'
    with lock.open('r') as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        prompt=sys.stdin.read()
        with Path(state['argv_log']).open('a') as log:
            print(json.dumps({'argv': argv, 'native': native, 'route': route, 'tools': tools,'input':prompt}), file=log)
        print(json.dumps({'type': 'session', 'sessionId': native['id'], 'cwd': native['cwd']}), flush=True)
        print(json.dumps({'type': 'status', 'phase': 'turn_start', 'turn': 1}), flush=True)
        atomic(state['ready'], {**native, 'pid': os.getpid()})
        deadline = time.monotonic() + 15
        while state.get('hold') and not Path(state['release']).exists():
            if time.monotonic() >= deadline:
                raise TimeoutError('The isolated test did not release its review transport')
            time.sleep(0.01)
        record_session(state['home'], native, 'completed', state['text'], route)
        print(json.dumps({'type': 'status', 'phase': 'turn_end', 'turn': 1,
                          'reason': {'kind': 'completed'}}), flush=True)
        print(json.dumps({'type': 'final', 'sessionId': native['id'],
                          'reason': {'kind': 'completed'}, 'text': state['text']}), flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main(sys.argv[1:]))
