#!/usr/bin/env python3
"""Devin lifecycle envelope for the shared, read-only harness snapshot."""
import contextlib
import hashlib
import json
import os
from pathlib import Path
import signal
import sys

import harness_snapshot as snapshot
import harness_tasks as tasks
from execution_lease import FileLock, atomic_json


def emit(root, request_id, run_id, event):
    name = event.get('hook_event_name')
    if name not in ('SessionStart', 'UserPromptSubmit', 'PostToolUse', 'PostCompaction'):
        return {}
    session = event.get('session_id')
    with contextlib.closing(tasks._connect(root, True, timeout=.1)) as connection:
        run = tasks._row(connection, 'work_runs', run_id)
        if run['request_id'] != request_id or not run['native_identity_json']:
            return {}  # SessionStart can precede the first observed native binding.
        native = json.loads(run['native_identity_json'])
    if native['host'] != 'devin_cli' or native['id'] != session:
        return {}
    value = snapshot.bundle(root, native, request_id=request_id)
    key = hashlib.sha256(tasks._json([native['host'], session, request_id]).encode()).hexdigest()
    path = root / '.control/hook-cache' / (key + '.json')
    with FileLock(root, ['context-cache', key]):
        try:
            cache = json.loads(path.read_bytes())
        except (OSError, ValueError):
            cache = {'epoch': 0}
        if name in ('SessionStart', 'PostCompaction'):
            cache['epoch'] = cache.get('epoch', 0) + 1
        if name == 'PostCompaction':
            # Native docs do not promise context output on this event. Invalidate
            # and restore at the next supported tool/prompt boundary instead.
            cache.pop('last', None)
            atomic_json(path, cache)
            return {}
        signature = [value['snapshot_hash'], cache.get('epoch', 0), run_id]
        if name != 'SessionStart' and cache.get('last') == signature:
            return {}
        content = snapshot.render(value)
        if len(content.encode()) > snapshot.HOOK_INLINE_BUDGET_BYTES:
            raise ValueError('Shared snapshot exceeded its complete-block presentation budget')
        atomic_json(path, {'epoch': cache.get('epoch', 0), 'last': signature})
        return {'hookSpecificOutput': {'hookEventName': name, 'additionalContext': content}}


def main():
    root = Path(os.environ.get('HTAM_DEVIN_ROOT', '')).resolve(strict=True)
    if root != Path(__file__).resolve().parent.parent:
        return {}
    return emit(root, os.environ['HTAM_DEVIN_REQUEST'], os.environ['HTAM_DEVIN_RUN'], json.load(sys.stdin))


if __name__ == '__main__':
    signal.signal(signal.SIGALRM, lambda *_: (_ for _ in ()).throw(TimeoutError('Hook read exceeded one second')))
    signal.setitimer(signal.ITIMER_REAL, 1)
    try:
        result = main()
    except (ValueError, OSError, KeyError, TypeError, tasks.sqlite3.Error, TimeoutError):
        result = {}
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
    print(json.dumps(result, ensure_ascii=False))
