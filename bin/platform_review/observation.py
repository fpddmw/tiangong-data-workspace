"""Read original board evidence and exact platform captures; no planning writer."""
import json
from pathlib import Path
import re

from workspace_files import safe_path as workspace_path, sha256 as digest, sha256_file

class BoardError(ValueError):
    pass

def require(condition, message):
    if not condition:
        raise BoardError(message)


def nonempty(value):
    return isinstance(value, str) and bool(value.strip())


def json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, indent=2) + '\n').encode()


def read_json(path):
    try:
        require(path.is_file(), f'Expected a regular file: {path}')
        return json.loads(path.read_bytes(), parse_constant=lambda value: (_ for _ in ()).throw(ValueError(f'Nonfinite JSON value: {value}')))
    except (OSError, ValueError) as exc:
        raise BoardError(f'Cannot read JSON: {path}: {exc}') from exc


def safe_path(root, value, *, must_exist=True):
    require(nonempty(value), 'Expected workspace file path')
    path = workspace_path(root, value, must_exist=must_exist)
    relative = path.relative_to(root.resolve())
    require(not any(part.startswith('.env') or part in ('.secrets', '.state', '.foundry', '.runtime', '.toolchain')
                    or part.endswith(('.sqlite', '.sqlite-wal', '.sqlite-shm')) for part in relative.parts),
            'Native state, credentials and runtime files remain with their owner')
    return path


def validate_ref(root, ref, *, nullable=False):
    if ref is None and nullable:
        return
    require(isinstance(ref, dict) and set(ref) == {'path', 'sha256'}, 'Reference requires path and byte sha256')
    require(isinstance(ref['sha256'], str) and re.fullmatch(r'[a-f0-9]{64}', ref['sha256']), 'Invalid reference digest')
    require(nonempty(ref['path']) and not Path(ref['path']).is_absolute() and '..' not in Path(ref['path']).parts, 'Invalid reference path')
    if root is None:
        return
    path = safe_path(root, ref['path'])
    require(path.is_file(), f'Missing referenced file: {ref["path"]}')
    require(sha256_file(path) == ref['sha256'], f'Stale reference: {ref["path"]}')


def find_task(board, task_id):
    found = [t for t in board['tasks'] if t['task_id'] == task_id]
    require(len(found) == 1, 'Task not found')
    return found[0]


def observe_package(root, package_id, subject_refs, capture_path, *, offline=False, previous_path=None,
                    expected_account_user_id=None):
    """Shared exact-target read, independent of the retired board's planning ledger."""
    import review_adapter as adapter
    root = Path(root).resolve()
    def observation_file(value):
        return safe_path(root, str(value))
    capture_file = observation_file(capture_path)
    capture = adapter.load_capture(capture_file)
    require(not adapter.validate_capture(capture), 'Invalid native read capture')
    table_types = {'processes': 'process', 'flows': 'flow', 'sources': 'source', 'contacts': 'contact',
                   'unitgroups': 'unitgroup', 'flowproperties': 'flowproperty', 'lifecyclemodels': 'lifecyclemodel'}
    targets = {f"{table_types[t['table']]}:{t['id']}@{t['version']}" for t in capture['scope']['targets']}
    if expected_account_user_id is not None:
        require(re.fullmatch(r'[a-f0-9]{8}-(?:[a-f0-9]{4}-){3}[a-f0-9]{12}', expected_account_user_id),
                'Expected observation account must be a canonical UUID')
        require(capture['source']['account_user_id'] == expected_account_user_id, 'Observation account does not match explicit expectation')
    previous = None
    if previous_path is not None:
        previous_file = observation_file(previous_path)
        previous = adapter.load_capture(previous_file)
    observation = adapter.assess_capture(capture, previous=previous)
    observation['evidence_gaps'].extend('target_not_read:' + target for target in sorted(set(subject_refs) - targets))
    observation['evidence_gaps'].extend('target_outside_selected_scope:' + target for target in sorted(targets - set(subject_refs)))
    return {'schema': 'data-workspace.package-observation.v1', 'package_id': package_id,
            'capture_ref': {'path': str(capture_file.relative_to(root)), 'sha256': digest(capture_file.read_bytes())},
            'observation': observation, 'board_writes': 0, 'platform_writes': 0,
            'authority': 'Read-only dated observation. No claim, transition, dispatch or native operation is admitted.'}
