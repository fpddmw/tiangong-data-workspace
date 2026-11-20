#!/usr/bin/env python3
"""One real ephemeral, tool-free reviewer under the authorized test parent."""
from pathlib import Path
import json
import sys
import time
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'bin'))
import production as entry
from production_runtime import Service
from production_review import request
from codex_host import CodexHost

ROOT = Path(__file__).resolve().parents[1]
BASE = ROOT / 'materials/packages/workspace-upgrade-20261007/host-qualification'

def main():
    if sys.argv[1:] != ['--same-authorized-thread']:raise SystemExit('Explicit synthetic test only')
    tid = entry.read_json(BASE / 'host-binding.json')['thread_id']
    goalfile = BASE / 'readonly-review-goal.md'
    goalfile.write_text('Architecture acceptance only: independently review one synthetic arithmetic package. No data access, no business execution.\n')
    goal = entry.resolve_goal(ROOT, str(goalfile))
    with entry.control_lock(ROOT, goal) as path:
        entry.atomic_write(path, {'schema': entry.CONTROL_SCHEMA, 'goal_path': goal['path'], 'goal_sha256_at_intent': goal['sha256'], 'revision': 1, 'intent': 'active'})
    package = BASE / 'synthetic-review-package.json'
    package.write_text(json.dumps({'purpose': 'synthetic review interface verification, not LCA data', 'author_thread_id': tid, 'claims': [{'claim': 'mass is conserved', 'input_kg': 10, 'product_kg': 9, 'waste_kg': 2}], 'acceptance': 'Review all evidence; no other evidence exists. Identify whether 10 = 9 + 2.'}))
    bound = {'thread_id': tid, 'cwd': str(BASE)}
    service = Service(ROOT, goal, bound)
    host = CodexHost(on_event=service.event, profile='native-single-writer')
    service.host = host
    try:
        msg = {'author_thread_id': tid, 'caller_thread_id': tid, 'request_id': 'synthetic-complete-review-v2',
               'input': str(package.relative_to(ROOT)), 'input_sha256': entry.sha256(package.read_bytes()),
               'out': str((BASE / 'readonly-review-opinion-v2.json').relative_to(ROOT))}
        result = request(service, msg)
        proof = {'passed': result['reviewer_thread_id'] != tid and result['opinion']['verdict'] == 'revise',
                 'author_thread_id': tid, 'reviewer_thread_id': result['reviewer_thread_id'],
                 'native_status': result['native_status'], 'input_sha256': result['input_sha256'],
                 'opinion_path': msg['out'], 'tools_observed': [r.unexpected_tool for r in service.reviews.values() if r.unexpected_tool],
                 'persisted_user_thread_created': False, 'mcp_isolation': [r.request.get('mcp_isolation') for r in service.reviews.values()]}
        entry.atomic_write(BASE / 'readonly-review.json', proof)
        print(json.dumps(proof), flush=True)
    finally:
        entry.stop(ROOT, goal, reason='Synthetic read-only review qualification ended')
        host.call('thread/goal/set', {'threadId': tid, 'status': 'paused', 'origin': 'user'})
        host.close()

if __name__ == '__main__':main()
