#!/usr/bin/env python3
"""Durable independent review transport; no author process/thread dependency."""
import argparse
import json
from pathlib import Path
import sys

from agent_base import child_environment
from execution_lease import FileLock
import agent_runtime
import harness_tasks as tasks
import production_review
import run_worker
from safe_diagnostics import retain, DIRECTORY


def run(root, parent_request, review_request, config):
    root = Path(root).resolve(strict=True)
    parent = tasks.read_request(root, parent_request)
    review = tasks.read_request(root, review_request)
    if not review['independent'] or review['parent_request_id'] != parent['id'] or review['task_id'] != parent['task_id']:
        raise ValueError('Review must retain its original author request and independent responsibility.')
    home = production_review.program_home(parent, root=root) / 'reviews' / review['id']
    with FileLock(root, ['review-job', review['id']]):
        # Original trusted launcher fixed both the packet and exact runtime.
        # This local process records its actual identity; it never claims the author.
        launch = json.loads((home / 'launch.json').read_bytes())
        ref = tasks.evidence_ref(root, launch['config_ref'])
        if Path(ref['path']).resolve() != Path(config).resolve():
            raise ValueError('Review runtime differs from the original launch intent.')
        if run_worker._selected_run(root, review['id']):
            return {'review_id': review['id'], 'state': 'original_run_exists'}
        worker = agent_runtime.Runtime(root)
        selected = agent_runtime.load_config(root, config, 'scientific_reviewer')
        return run_worker._program_turn(root, worker, parent, review, selected)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for key in ('root', 'parent-request', 'review-request', 'config'):
        parser.add_argument('--' + key, required=True)
    args = parser.parse_args()
    directory = production_review.program_home(tasks.read_request(Path(args.root), args.parent_request), root=Path(args.root)) / 'reviews' / args.review_request
    token = DIRECTORY.set(str(directory / 'diagnostics'))
    try:
        print(json.dumps(run(args.root, args.parent_request, args.review_request, args.config), ensure_ascii=False))
    except Exception as exc:
        print(json.dumps(retain(str(exc), source='review_job', code=type(exc).__name__)), file=sys.stderr)
        return 1
    finally:
        DIRECTORY.reset(token)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
