#!/usr/bin/env python3
"""Original business/legacy handoff links in the existing task database.

No copied native owner, attempt or accepted flag. Views read the original
receipts; an organizational use decision is distinct from a professional receipt.
"""
import hashlib
import json
import re
from pathlib import Path
import harness_tasks as tasks


def install_schema(c):
    tasks.add_schema(c, [
        """CREATE TABLE IF NOT EXISTS work_original_links(
           id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES work_tasks(id),
           kind TEXT NOT NULL, original_id TEXT NOT NULL, source_ref_json TEXT,
           scope_json TEXT NOT NULL, actor_json TEXT NOT NULL, created_at TEXT NOT NULL,
           UNIQUE(task_id,kind,original_id))""",
        """CREATE TABLE IF NOT EXISTS work_handoff_uses(
           id TEXT PRIMARY KEY, link_id TEXT NOT NULL REFERENCES work_original_links(id),
           decision_ref_json TEXT NOT NULL, evidence_ref_json TEXT NOT NULL,
           actor_json TEXT NOT NULL, scope_json TEXT NOT NULL, conditions_json TEXT NOT NULL,
           plan_revision INTEGER NOT NULL, created_at TEXT NOT NULL)""",
        """CREATE TABLE IF NOT EXISTS work_board_cutovers(
           board_path TEXT PRIMARY KEY, validation_ref_json TEXT NOT NULL,
           actor_json TEXT NOT NULL, source_ref_json TEXT NOT NULL, created_at TEXT NOT NULL)""",
        """CREATE TABLE IF NOT EXISTS work_business_observations(
           id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES work_tasks(id),
           link_id TEXT NOT NULL REFERENCES work_original_links(id), capture_ref_json TEXT NOT NULL,
           account_user_id TEXT, plan_revision INTEGER NOT NULL, actor_json TEXT NOT NULL, created_at TEXT NOT NULL)"""
    ], ('work_original_links', 'work_handoff_uses', 'work_board_cutovers', 'work_business_observations'))


def apply(c, root, action, actor, payload):
    ident = tasks._text(payload.get('id'), 'Stable relation id')
    if action == 'business-bind':
        task = tasks._row(c, 'work_tasks', payload.get('task_id'))
        ref, package = read_package(root, payload.get('source_ref'))
        fields = {'task_id': task['id'], 'kind': 'business', 'original_id': package['package_id'],
                  'source_ref_json': tasks._json(ref), 'scope_json': tasks._json(package['scope']),
                  'actor_json': tasks._json(actor)}
        previous = c.execute("SELECT * FROM work_original_links WHERE task_id=? AND kind='business' AND original_id=?",
                             (task['id'], package['package_id'])).fetchone()
        if previous:
            if previous['source_ref_json'] != fields['source_ref_json']:
                tasks._event(c, task, 'business-binding-updated', actor,
                             {'link_id': previous['id'], 'previous': tasks._decode(previous), 'source_ref': ref})
                c.execute('UPDATE work_original_links SET source_ref_json=?,scope_json=?,actor_json=? WHERE id=?',
                          (fields['source_ref_json'], fields['scope_json'], fields['actor_json'], previous['id']))
            return tasks._decode(tasks._row(c, 'work_original_links', previous['id']))
        return tasks._decode(tasks._immutable(c, 'work_original_links', ident, fields))
    if action == 'business-observe':
        task = tasks._row(c, 'work_tasks', payload.get('task_id'))
        links = package_links(c, task['id'])
        if payload.get('link_id'):
            links = [link for link in links if link['id'] == payload['link_id']]
        if payload.get('package_id'):
            links = [link for link in links if link['original_id'] == payload['package_id']]
        if len(links) != 1:
            raise ValueError('Select one exact original business package link')
        capture = tasks.evidence_ref(root, payload.get('capture_ref'))
        _observe(root, links[0], capture, payload.get('expected_account_user_id'))
        fields = {'task_id': task['id'], 'link_id': links[0]['id'], 'capture_ref_json': tasks._json(capture),
                  'account_user_id': payload.get('expected_account_user_id'), 'plan_revision': task['plan_revision'],
                  'actor_json': tasks._json(actor)}
        return tasks._decode(tasks._immutable(c, 'work_business_observations', ident, fields))
    raise ValueError('Unknown original-link action')


def project(c, root, task_id):
    if not tasks._exists(c, 'work_original_links'):
        return []
    result = []
    for row in c.execute('SELECT * FROM work_original_links WHERE task_id=? ORDER BY created_at,id', (task_id,)):
        item = tasks._decode(row)
        item['uses'] = []
        for use in c.execute('SELECT * FROM work_handoff_uses WHERE link_id=? ORDER BY created_at,id', (row['id'],)):
            detail = tasks._decode(use)
            item['uses'].append(detail)
        try:
            if row['kind'] == 'handoff':
                original = tasks._row(c, 'handoffs', row['original_id'])
                item['original'] = dict(original)
                item['inspection_command'] = 'python3 bin/harness.py history --table handoffs --id ' + original['id']
            elif row['kind'] == 'business':
                ref, item['original'] = read_package(root, json.loads(row['source_ref_json']))
                item['meaning'] = 'Exact business targets; owner and plan remain on the original complete task.'
            else:
                ref = tasks.evidence_ref(root, json.loads(row['source_ref_json']))
                source = json.loads(Path(ref['path']).read_bytes())
                import business_board as board
                item['original'] = board.find_task(source, row['original_id'])
                item['mode'] = source['mode']
                item['meaning'] = 'Frozen business observation; current task owner and adoption come from SQLite.'
            item['integrity'] = 'verified'
        except (OSError, ValueError, KeyError) as exc:
            item['integrity'] = 'unavailable_or_changed'
            item['error'] = str(exc)
        result.append(item)
    return result


def package_links(c, task_id):
    return c.execute("SELECT * FROM work_original_links WHERE task_id=? AND kind IN ('board','business') ORDER BY created_at,id",
                     (task_id,)).fetchall() if tasks._exists(c, 'work_original_links') else []


def read_package(root, source_ref):
    ref = tasks.evidence_ref(root, source_ref)
    package = json.loads(Path(ref['path']).read_bytes())
    if not isinstance(package, dict):
        raise ValueError('Read a package description with its package_id and selected subject_refs')
    package = {**package, 'scope': package.get('scope', [])}
    tasks._text(package['package_id'], 'Original package identity/version')
    tasks._list(package['scope'], 'Business package scope')
    subjects = tasks._list(package['subject_refs'], 'Exact business targets')
    pattern = r'(?:process|flow|source|contact|unitgroup|flowproperty|lifecyclemodel):[^\s:@]+@\d{2}\.\d{2}\.\d{3}'
    if any(not re.fullmatch(pattern, subject) for subject in subjects):
        raise ValueError('Each subject_ref must name an exact type:id@NN.NN.NNN target')
    return ref, package


def bind_package(root, actor, task_id, path):
    ref, _ = tasks._saved_document(root, path)
    task = tasks.read_task(root, task_id)['task']
    return tasks.mutate(root, 'business-bind', actor, {
        'id': 'package:' + hashlib.sha256(tasks._json([task_id, ref]).encode()).hexdigest(),
        'task_id': task_id, 'source_ref': ref,
        'expected_task_revision': task['revision'], 'plan_revision': task['plan_revision']})


def observe_package(root, actor, task_id, path, package_id=None, account=None):
    import business_board
    root = Path(root).resolve()
    selected = Path(path)
    if selected.is_absolute():
        selected = selected.relative_to(root)
    selected = business_board.safe_path(root, str(selected))
    ref = tasks.evidence_ref(root, {'path': str(selected), 'sha256': hashlib.sha256(selected.read_bytes()).hexdigest()})
    with tasks.transaction(root) as c:
        task = tasks._row(c, 'work_tasks', task_id)
        payload = {'task_id': task_id, 'package_id': package_id, 'capture_ref': ref,
                   'expected_account_user_id': account}
        payload['id'] = 'observation:' + hashlib.sha256(tasks._json([payload, task['plan_revision']]).encode()).hexdigest()
        return tasks.apply(c, root, 'business-observe', actor, payload)


def _observe(root, link, capture_ref, account):
    import business_board as board
    ref = tasks.evidence_ref(root, json.loads(link['source_ref_json']))
    original = json.loads(Path(ref['path']).read_bytes())
    package = original if link['kind'] == 'business' else board.find_task(original, link['original_id'])
    capture = tasks.evidence_ref(root, capture_ref)
    return board.observe_package(root, link['original_id'], package['subject_refs'], capture['path'],
                                 expected_account_user_id=account)


def business_projection(c, root, task):
    packages = []
    for link in package_links(c, task['id']):
        observations = [tasks._decode(row) for row in c.execute(
            'SELECT * FROM work_business_observations WHERE link_id=? ORDER BY created_at,id', (link['id'],))]
        packages.append({'original_id': link['original_id'], 'source_ref': json.loads(link['source_ref_json']),
                         'observations': observations})
    return {'packages': packages, 'authority': 'Original package and dated native observations; read the exact capture for platform facts.'}


def board_view(root):
    return tasks.current_view(root, include_closed=True, limit=100)
