#!/usr/bin/env python3
"""Discover original methods and their use; store references in the task database.

The relation index is rebuildable. Method prose, professional checkpoints and
scientific decisions remain with their original files and native receipts.
"""
import argparse
from contextlib import contextmanager
import hashlib
import json
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RELATIONS = ('uses', 'condition', 'counterexample', 'replaces', 'candidate',
             'approved', 'runtime', 'effect', 'retired')


def core():
    import harness_tasks
    return harness_tasks


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def reference_key(root, ref):
    if (not isinstance(ref, dict) or not isinstance(ref.get('path'), str)
            or not isinstance(ref.get('sha256'), str) or len(ref['sha256']) != 64
            or any(c not in '0123456789abcdef' for c in ref['sha256'])):
        raise ValueError('Exact method identity requires its original reference')
    path = Path(ref['path'])
    path = path if path.is_absolute() else Path(root) / path
    return canonical([str(path.resolve()), ref['sha256']])


def source(root, ref, *, structured=False):
    """Read only an exact selected original, never search archive or copy prose."""
    if not isinstance(ref, dict) or not isinstance(ref.get('path'), str):
        raise ValueError('Original reference requires path and sha256')
    expected = ref.get('sha256')
    if (not isinstance(expected, str) or len(expected) != 64
            or any(c not in '0123456789abcdef' for c in expected)):
        raise ValueError('Original reference requires an exact sha256')
    # The original harness owns credential exclusions and same-byte relocation.
    path = Path(core().evidence_ref(root, ref)['path'])
    payload = path.read_bytes()
    if hashlib.sha256(payload).hexdigest() != expected:
        raise ValueError('Original source changed; select the intended version explicitly')
    return json.loads(payload) if structured else payload


def pointer(value, path):
    if path == '':
        return value
    if not isinstance(path, str) or not path.startswith('/'):
        raise ValueError('Select an exact JSON pointer in the original receipt')
    try:
        for part in path[1:].split('/'):
            part = part.replace('~1', '/').replace('~0', '~')
            value = value[int(part)] if isinstance(value, list) else value[part]
        return value
    except (IndexError, KeyError, TypeError, ValueError) as exc:
        raise ValueError(f'Original JSON pointer is unavailable: {path}') from exc


def install_schema(c):
    core().add_schema(c, [
        '''CREATE TABLE IF NOT EXISTS work_knowledge_links (
           id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES work_tasks(id),
           method_ref_json TEXT NOT NULL, version TEXT NOT NULL, relation TEXT NOT NULL,
           target_ref_json TEXT NOT NULL, conditions_json TEXT NOT NULL,
           required INTEGER NOT NULL, evidence_ref_json TEXT NOT NULL,
           evidence_pointer TEXT NOT NULL, actor_json TEXT NOT NULL,
           method_key TEXT NOT NULL, target_key TEXT NOT NULL,
           UNIQUE(task_id,evidence_ref_json,evidence_pointer))''',
        'CREATE INDEX IF NOT EXISTS work_knowledge_method ON work_knowledge_links(method_key)',
        'CREATE INDEX IF NOT EXISTS work_knowledge_target ON work_knowledge_links(target_key)'
    ], ['work_knowledge_links'])


def has_table(c, name):
    return bool(c.execute('SELECT 1 FROM sqlite_master WHERE type=\'table\' AND name=?', (name,)).fetchone())


@contextmanager
def read_connection(root):
    path = Path(root).resolve() / '.control/harness.sqlite'
    if not path.is_file():
        raise ValueError('Task database is missing; run explicit work-init only with approved original evidence')
    c = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)
    c.row_factory = sqlite3.Row
    c.execute('PRAGMA query_only=ON')
    try:
        yield c
    finally:
        c.close()


def task_sources(snapshot):
    """Use declared original/plan/return refs; do not recursively scan materials."""
    result = []
    task = snapshot['task']
    for name in ('original_ref', 'plan_ref'):
        if isinstance(task.get(name), dict):
            result.append(task[name])
    for request in snapshot.get('requests', []):
        if isinstance(request.get('input_ref'), dict):
            result.append(request['input_ref'])
    for returned in snapshot.get('returns', []):
        for name in ('output_ref', 'result_ref', 'manifest_ref'):
            if isinstance(returned.get(name), dict):
                result.append(returned[name])
        for name in ('artifact_refs', 'professional_refs', 'receipt_refs', 'optional_correction_refs'):
            if isinstance(returned.get(name), list):
                result.extend(r for r in returned[name] if isinstance(r, dict) and 'sha256' in r)
    return list({canonical(ref): ref for ref in result}.values())


def validate_link(root, item, *, require_available=True):
    if not isinstance(item, dict):
        raise ValueError('Reference must be an object.')
    for field in ('method_ref', 'target_ref'):
        reference_key(root, item[field])
        if require_available:
            source(root, item[field])
    return item


def index_original(c, root, task_id, source_ref, actor):
    """Project explicit original metadata during the core's existing mutation.

    Return actionable warnings, preserving actual delivery even when discovery
    metadata is incomplete. This helper never turns source claims into approval.
    """
    if not has_table(c, 'work_knowledge_links'):
        return [{'source_ref': source_ref, 'reason': 'Knowledge schema unavailable; explicit work-init is required'}]
    try:
        original = json.loads(source(root, source_ref))
        doc = original
    except (ValueError, OSError, UnicodeError) as exc:
        return ([{'source_ref': source_ref, 'reason': str(exc)}]
                if str(source_ref.get('path', '')).endswith('.json') else [])
    warnings = []
    links = doc.get('knowledge_links', []) if isinstance(doc, dict) else []
    if not isinstance(links, list):
        warnings.append({'source_ref': source_ref, 'reason': 'Explicit knowledge_links is not a list'})
        links = []
    selected = [(f'/knowledge_links/{index}', item) for index, item in enumerate(links)]
    # The native adapter already observed the invocation; no model-authored
    # duplicate use form or outcome claim is required to make it discoverable.
    if (isinstance(original, dict) and original.get('schema') == 'data-workspace.agent-runtime-observation.v1'
            and original.get('source') == 'native_rpc' and isinstance(original.get('result'), dict)):
        for index, observed in enumerate(original['result'].get('tool_observations', [])):
            ref = observed.get('entry_ref', {})
            selected.append((f'/result/tool_observations/{index}', {
                'method_ref': ref, 'version': ref.get('sha256'), 'relation': 'runtime', 'target_ref': source_ref,
                'required': False, 'conditions': [observed.get('basis', 'Native invocation only'),
                    'Observed status: ' + str(observed.get('status')) + '; exit code: ' + str(observed.get('exit_code'))]}))
    # Explicit native Skill checkpoints carry their own output evidence. Index
    # these once as uses; this does not promote them to approved methods.
    skill_uses = doc.get('skill_uses', []) if isinstance(doc, dict) else []
    if not isinstance(skill_uses, list):
        warnings.append({'source_ref': source_ref, 'reason': 'Original skill_uses must be a list; delivery retained'})
        skill_uses = []
    for index, use in enumerate(skill_uses):
        if not isinstance(use, dict) or not isinstance(use.get('output_refs'), list) or not use['output_refs']:
            continue
        selected.append((f'/skill_uses/{index}', {'method_ref': use.get('skill_ref'), 'version': use.get('version'),
            'relation': 'uses', 'target_ref': source_ref, 'required': False,
            'conditions': ['Original Skill use declaration; inspect native output_refs and remaining_branches before reuse.']}))
    for pointer_path, item in selected:
        try:
            validate_link(root, item, require_available=False)
        except (ValueError, TypeError, KeyError) as exc:
            warnings.append({'source_ref': source_ref, 'pointer': pointer_path, 'reason': str(exc)})
            continue
        identity = hashlib.sha256(canonical([task_id, reference_key(root, source_ref), pointer_path]).encode()).hexdigest()
        evidence = {'path': str((Path(root) / source_ref['path']).resolve()), 'sha256': source_ref['sha256']}
        c.execute('INSERT OR IGNORE INTO work_knowledge_links VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',
            (identity, task_id, canonical(item['method_ref']), item.get('version', item['method_ref']['sha256']), item.get('relation', 'uses'),
             canonical(item['target_ref']), canonical(item.get('conditions', [])), int(item.get('required', False)),
              canonical(evidence), pointer_path, canonical(actor),
             reference_key(root, item['method_ref']), reference_key(root, item['target_ref'])))
    return warnings


def record_links(root, actor, task_id, source_ref):
    """Index optional links from an explicitly selected original, retaining prose."""
    core().read_task(root, task_id)
    source(root, source_ref)
    with core().transaction(root) as c:
        if not has_table(c, 'work_knowledge_links'):
            raise ValueError('Knowledge schema is unavailable; explicit work-init is required')
        core().verify_actor(root, actor)
        core()._row(c, 'work_tasks', task_id)
        warnings = index_original(c, root, task_id, source_ref, actor)
        count = c.execute('SELECT COUNT(*) FROM work_knowledge_links WHERE task_id=? AND evidence_ref_json=?',
                          (task_id, canonical({'path': str((Path(root) / source_ref['path']).resolve()), 'sha256': source_ref['sha256']}))).fetchone()[0]
    return {'task_id': task_id, 'indexed': count, 'source_ref': source_ref, 'warnings': warnings,
            'meaning': 'Original relations indexed; reading or indexing is not professional completion or scientific adoption.'}


def integrity(root, ref):
    try:
        source(root, ref)
        return 'verified'
    except (OSError, ValueError, TypeError) as exc:
        return {'status': 'unavailable_or_changed', 'reason': str(exc)}


def project_link(root, row):
    item = {k: row[k] for k in ('id', 'task_id', 'version', 'relation', 'evidence_pointer')}
    for name in ('method_ref', 'target_ref', 'conditions', 'evidence_ref'):
        item[name] = json.loads(row[name + '_json'])
    item['required'] = bool(row['required'])
    item['integrity'] = {name: integrity(root, item[name]) for name in ('method_ref', 'target_ref', 'evidence_ref')}
    return item


def discover(root, task_id, *, query=None, limit=10, offset=0):
    from workspace_queries import discovery as harness_discovery
    if not 1 <= limit <= 100 or offset < 0:
        raise ValueError('Use limit 1..100 and nonnegative offset.')
    snapshot = core().read_task(root, task_id)
    with read_connection(root) as c:
        rows = c.execute('SELECT * FROM work_knowledge_links ' + ('' if query else 'WHERE task_id=? ') +
                         'ORDER BY id', () if query else (task_id,)).fetchall()
    links = [project_link(root, row) for row in rows
             if not query or harness_discovery.score(query, canonical(dict(row)))]
    return {'status': 'read_only', 'task_id': task_id, 'originals': task_sources(snapshot),
            'links': links[offset:offset + limit], 'total': len(links), 'offset': offset,
            'next_offset': offset + limit if offset + limit < len(links) else None}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=ROOT)
    sub = parser.add_subparsers(dest='command', required=True)
    q = sub.add_parser('discover'); q.add_argument('--task', required=True); q.add_argument('--query'); q.add_argument('--limit', type=int, default=5); q.add_argument('--offset', type=int, default=0)
    q = sub.add_parser('index'); q.add_argument('--task', required=True); q.add_argument('--source', required=True); q.add_argument('--sha256', required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == 'discover':
            value = discover(args.root, args.task, query=args.query, limit=args.limit, offset=args.offset)
        else:
            value = record_links(args.root, core().verified_actor(args.root), args.task,
                                 {'path': args.source, 'sha256': args.sha256})
        print(json.dumps(value, ensure_ascii=False, indent=2)); return 0
    except (ValueError, OSError, sqlite3.Error) as exc:
        print(json.dumps({'status': 'BLOCKED', 'error': str(exc)})); return 1


if __name__ == '__main__':
    raise SystemExit(main())
