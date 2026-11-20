#!/usr/bin/env python3
"""Real read-only reviewer interrupt + same-request retry, no data operations."""
from pathlib import Path
import json
import sys
import threading
import time
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'bin'))
import production as entry
from production_runtime import Service
from production_review import request
from codex_host import CodexHost
ROOT = Path(__file__).resolve().parents[1]
BASE = ROOT / 'materials/packages/workspace-upgrade-20261007/host-qualification'

def main():
    if sys.argv[1:] != ['--same-authorized-thread']:raise SystemExit('Synthetic authorization required')
    tid = entry.read_json(BASE / 'host-binding.json')['thread_id']
    goal = entry.resolve_goal(ROOT, str(BASE / 'readonly-review-goal.md'))
    with entry.control_lock(ROOT, goal) as path:
        old = entry.load_control(ROOT, goal); old['intent'] = 'active'; old['revision'] += 1; entry.atomic_write(path, old)
    service = Service(ROOT, goal, {'thread_id': tid, 'cwd': str(BASE)})
    host = CodexHost(on_event=service.event, profile='native-single-writer'); service.host = host
    package = BASE / 'synthetic-review-package.json'
    msg = {'author_thread_id': tid, 'caller_thread_id': tid, 'request_id': 'synthetic-stopped-review-v2',
           'input': str(package.relative_to(ROOT)), 'input_sha256': entry.sha256(package.read_bytes()),
           'out': str((BASE / 'readonly-review-retry-opinion-v2.json').relative_to(ROOT))}
    failures = []
    def worker():
        try:request(service, msg)
        except Exception as exc:failures.append(type(exc).__name__ + ': ' + str(exc))
    worker_thread = threading.Thread(target=worker)
    worker_thread.start()
    try:
        deadline = time.monotonic() + 60
        original = None
        while time.monotonic() < deadline:
            original = service.reviews.get(msg['request_id'])
            if original and original.turn_id:break
            time.sleep(.02)
        if not original or not original.turn_id:raise RuntimeError('No original reviewer native turn')
        entry.stop(ROOT, goal, reason='Actual synthetic reviewer interruption')
        stopped = service.stop()
        worker_thread.join(40)
        if worker_thread.is_alive():raise RuntimeError('Original reviewer did not settle')
        denied = dict(msg, request_id='synthetic-denied-after-stop-v2', out=str((BASE / 'should-not-exist.json').relative_to(ROOT)))
        try:request(service, denied)
        except entry.ProductionError:pass
        else:raise RuntimeError('Reviewer admitted after persistent pause')
        denied_review = service.reviews[denied['request_id']]
        with entry.control_lock(ROOT, goal) as path:
            old = entry.load_control(ROOT, goal); old['intent'] = 'active'; old['revision'] += 1; entry.atomic_write(path, old)
        service.done.clear(); service.finished_cleanly = False
        result = request(service, dict(msg, retry_attempt=1))
        proof = {'passed': stopped['native_parent_dispatch_stopped'] and original.status == 'interrupted'
                 and denied_review.thread_id is None and result['attempt'] == 2 and result['reviewer_thread_id'] != original.thread_id,
                 'author_thread_id': tid, 'first_reviewer': original.thread_id, 'first_status': original.status,
                 'denied_after_stop_before_thread_creation': denied_review.thread_id is None,
                 'retry_reviewer': result['reviewer_thread_id'], 'same_request_id': result['request_id'], 'retry_attempt': result['attempt'],
                 'stop_receipt': stopped, 'original_error': failures, 'retry_opinion': result['opinion']}
        entry.atomic_write(BASE / 'readonly-review-stop.json', proof)
        print(json.dumps({'passed': proof['passed'], 'first_status': proof['first_status'], 'retry_attempt': proof['retry_attempt']}), flush=True)
    finally:
        entry.stop(ROOT, goal, reason='Synthetic reviewer test ended')
        for review in service.reviews.values():
            if review.thread_id and not review.completed.is_set():review.interrupt()
        paused = host.call('thread/goal/set', {'threadId': tid, 'status': 'paused', 'origin': 'user'})
        host.close()
        entry.atomic_write(BASE / 'original-host-handoff.json', {'thread_id': tid, 'native_goal': paused,
                           'original_host_closed': host.process.poll() is not None, 'scope': 'single authorized synthetic architecture thread only'})

if __name__ == '__main__':main()
