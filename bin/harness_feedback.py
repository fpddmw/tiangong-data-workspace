#!/usr/bin/env python3
"""Finite original-receipt feedback ingestion and existing maintenance links.

No network collector, schedule, scientific decision or remote action is implied.
An explicit mapping selects fields from a supported tool's actual saved receipt;
this module neither invents an endpoint nor normalizes native platform states.
"""
import argparse
from datetime import datetime
import hashlib
import json
import shlex
import sqlite3
from pathlib import Path

import harness_knowledge as knowledge

ROOT = Path(__file__).resolve().parent.parent
DISPOSITIONS = ('revise_data', 'clarify_only', 'not_adopted_with_evidence', 'needs_clarification')


def core():
    import harness_tasks
    return harness_tasks


def install_schema(c):
    core().add_schema(c, [
        '''CREATE TABLE IF NOT EXISTS work_feedback_pages (
           id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES work_tasks(id),
           source TEXT NOT NULL, review_id TEXT NOT NULL, cursor_json TEXT NOT NULL,
           next_cursor_json TEXT NOT NULL, source_ref_json TEXT NOT NULL,
           mapping_json TEXT NOT NULL, observed_at TEXT NOT NULL, actor_json TEXT NOT NULL)''',
        '''CREATE TABLE IF NOT EXISTS work_feedback_cursors (
           task_id TEXT NOT NULL REFERENCES work_tasks(id), source TEXT NOT NULL,
           review_id TEXT NOT NULL, cursor_json TEXT NOT NULL, page_id TEXT NOT NULL
           REFERENCES work_feedback_pages(id), observed_at TEXT NOT NULL,
           PRIMARY KEY(task_id,source,review_id))''',
        '''CREATE TABLE IF NOT EXISTS work_feedback_observations (
           sequence INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT NOT NULL UNIQUE,
           task_id TEXT NOT NULL REFERENCES work_tasks(id), source TEXT NOT NULL,
           review_id TEXT NOT NULL, dataset_id TEXT NOT NULL, dataset_version TEXT NOT NULL,
           opinion_id TEXT NOT NULL, opinion_version TEXT NOT NULL, content_sha256 TEXT NOT NULL,
           retracted INTEGER NOT NULL, source_ref_json TEXT NOT NULL,
           source_pointer TEXT NOT NULL, page_id TEXT NOT NULL REFERENCES work_feedback_pages(id),
           request_id TEXT NOT NULL REFERENCES work_requests(id), supersedes_request_id TEXT,
           observed_at TEXT NOT NULL)''',
        '''CREATE TABLE IF NOT EXISTS work_maintenance_links (
           request_id TEXT PRIMARY KEY REFERENCES work_requests(id),
           task_id TEXT NOT NULL REFERENCES work_tasks(id), engineering_id TEXT,
           source_ref_json TEXT NOT NULL, actor_json TEXT NOT NULL)'''
        ,'''CREATE TABLE IF NOT EXISTS work_feedback_dispositions (
           sequence INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT NOT NULL UNIQUE,
           request_id TEXT NOT NULL REFERENCES work_requests(id),
           observation_id TEXT NOT NULL REFERENCES work_feedback_observations(id),
           decision TEXT NOT NULL, evidence_ref_json TEXT NOT NULL, actor_json TEXT NOT NULL,
           supersedes_id TEXT REFERENCES work_feedback_dispositions(id),
           revision_request_id TEXT REFERENCES work_requests(id))'''
    ], ['work_feedback_pages', 'work_feedback_cursors', 'work_feedback_observations',
        'work_maintenance_links', 'work_feedback_dispositions'])


def require_schema(c):
    for name in ('work_feedback_pages', 'work_feedback_cursors', 'work_feedback_observations',
                 'work_maintenance_links', 'work_feedback_dispositions'):
        if not knowledge.has_table(c, name):
            raise ValueError('Feedback schema is unavailable; run explicit work-init with approved original evidence')


def string(value, name):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f'{name} must be a nonempty original string')
    return value


def timestamp(value):
    string(value, 'Observed time')
    try:
        observed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if observed.tzinfo is None:
            raise ValueError('Missing timezone')
        return observed
    except ValueError as exc:
        raise ValueError('Observed time must include its timezone') from exc


def parse_page(root, payload):
    """Read observed opinions; completeness controls only cursor advancement."""
    ref = core().evidence_ref(root, payload.get('source_ref'))
    receipt = knowledge.source(root, ref, structured=True)
    mapping = payload.get('mapping')
    required = ('review_id', 'dataset_id', 'dataset_version', 'items', 'opinion_id',
                'cursor', 'next_cursor', 'complete', 'observed_at')
    allowed = set(required) | {'opinion_version', 'retracted'}
    if (not isinstance(mapping, dict) or set(mapping) - allowed
            or any(name not in mapping for name in required)
            or not all(isinstance(p, str) for p in mapping.values())):
        raise ValueError('Supply the explicit native field mapping; no endpoint or receipt shape is assumed')
    get = lambda name: knowledge.pointer(receipt, mapping[name])
    complete = get('complete') is True
    review_id = string(get('review_id'), 'Review id')
    dataset_id = string(get('dataset_id'), 'Dataset id')
    dataset_version = string(get('dataset_version'), 'Dataset version')
    observed_at = get('observed_at'); timestamp(observed_at)
    cursor, next_cursor = get('cursor'), get('next_cursor')
    if not all(value is None or isinstance(value, str) for value in (cursor, next_cursor)):
        raise ValueError('Native cursor must be a string or null')
    if next_cursor is not None and cursor == next_cursor:
        raise ValueError('Native next cursor did not advance')
    rows = get('items')
    if not isinstance(rows, list):
        raise ValueError('Select the items of one complete native page')
    items, seen = [], set()
    for index, row in enumerate(rows):
        opinion = string(knowledge.pointer(row, mapping['opinion_id']), 'Opinion id')
        if opinion in seen:
            raise ValueError('Native page repeats an opinion identity')
        seen.add(opinion)
        fingerprint = hashlib.sha256(knowledge.canonical(row).encode()).hexdigest()
        version = knowledge.pointer(row, mapping['opinion_version']) if 'opinion_version' in mapping else fingerprint
        string(version, 'Opinion version or content fingerprint')
        retracted = knowledge.pointer(row, mapping['retracted']) if 'retracted' in mapping else False
        if not isinstance(retracted, bool):
            raise ValueError('Retraction mapping must select an explicit native boolean')
        items.append({'opinion_id': opinion, 'opinion_version': version,
                      'content_sha256': fingerprint, 'retracted': retracted,
                      'pointer': mapping['items'] + '/' + str(index)})
    return {'review_id': review_id, 'dataset_id': dataset_id, 'dataset_version': dataset_version,
            'observed_at': observed_at, 'cursor': cursor, 'next_cursor': next_cursor,
            'items': items, 'mapping': mapping, 'source_ref': ref, 'complete': complete}


def ingest_page(root, actor, payload):
    task_id = string(payload.get('task_id'), 'Original task id')
    origin = string(payload.get('source'), 'Native source identity')
    page = parse_page(root, payload)
    return _ingest(root, actor, task_id, origin, page)


def _ingest(root, actor, task_id, origin, page):
    snapshot = core().read_task(root, task_id)
    page_id = hashlib.sha256(knowledge.canonical([task_id, origin, page['review_id'],
                                               page['source_ref'], page['mapping']]).encode()).hexdigest()
    with core().transaction(root) as c:
        require_schema(c)
        core().verify_actor(root, actor)
        task = core()._row(c, 'work_tasks', task_id)
        existing = c.execute('SELECT id FROM work_feedback_pages WHERE id=?', (page_id,)).fetchone()
        if existing:
            return {'status': 'already_ingested', 'page_id': page_id, 'created_requests': []}
        prior_cursor = c.execute('SELECT * FROM work_feedback_cursors WHERE task_id=? AND source=? AND review_id=?',
                                 (task_id, origin, page['review_id'])).fetchone()
        collection_issues = []
        if prior_cursor:
            if json.loads(prior_cursor['cursor_json']) != page['cursor']:
                collection_issues.append('Page does not continue the last complete collection cursor')
            if timestamp(page['observed_at']) < timestamp(prior_cursor['observed_at']):
                collection_issues.append('Historical snapshot retained; current collection cursor unchanged')
        advance_cursor = page['complete'] and not collection_issues
        c.execute('INSERT INTO work_feedback_pages VALUES (?,?,?,?,?,?,?,?,?,?)',
                  (page_id, task_id, origin, page['review_id'], knowledge.canonical(page['cursor']),
                   knowledge.canonical(page['next_cursor']), knowledge.canonical(page['source_ref']),
                   knowledge.canonical(page['mapping']), page['observed_at'], knowledge.canonical(actor)))
        created, unchanged = [], []
        for item in page['items']:
            previous = c.execute('''SELECT * FROM work_feedback_observations WHERE
                task_id=? AND source=? AND review_id=? AND dataset_id=? AND opinion_id=?
                ORDER BY julianday(observed_at) DESC,sequence DESC LIMIT 1''', (task_id, origin, page['review_id'], page['dataset_id'], item['opinion_id'])).fetchone()
            signature = [task_id, origin, page['review_id'], page['dataset_id'], page['dataset_version'],
                         item['opinion_id'], item['opinion_version'], item['content_sha256'], item['retracted']]
            observation_id = hashlib.sha256(knowledge.canonical(signature).encode()).hexdigest()
            if previous and all(previous[key] == value for key, value in {
                'dataset_version': page['dataset_version'], 'opinion_version': item['opinion_version'],
                'content_sha256': item['content_sha256'], 'retracted': int(item['retracted'])}.items()):
                unchanged.append(previous['request_id']); continue
            # A content rollback is a new observation at its real time. Its
            # request identity includes this page; ordinary repeated pages do not.
            if c.execute('SELECT 1 FROM work_feedback_observations WHERE id=?', (observation_id,)).fetchone():
                observation_id = hashlib.sha256(knowledge.canonical([signature, page_id]).encode()).hexdigest()
            request_id = 'FB-' + observation_id[:32]
            request_payload = {'id': request_id, 'task_id': task_id, 'kind': 'feedback',
                'input_ref': page['source_ref'],
                'summary': f"External feedback {page['review_id']}/{item['opinion_id']} at {item['pointer']}; version {item['opinion_version']}",
                'recipient': actor, 'recipient_role': 'task_lead',
                'plan_revision': task['plan_revision'], 'required_scope': []}
            supersedes = previous['request_id'] if previous and timestamp(page['observed_at']) >= timestamp(previous['observed_at']) else None
            if supersedes:
                request_payload['supersedes_request_id'] = supersedes
            core().apply(c, root, 'request', actor, request_payload)
            c.execute('''INSERT INTO work_feedback_observations
                (id,task_id,source,review_id,dataset_id,dataset_version,opinion_id,opinion_version,
                 content_sha256,retracted,source_ref_json,source_pointer,page_id,request_id,supersedes_request_id,observed_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                (observation_id, task_id, origin, page['review_id'], page['dataset_id'], page['dataset_version'],
                 item['opinion_id'], item['opinion_version'], item['content_sha256'], int(item['retracted']),
                 knowledge.canonical(page['source_ref']), item['pointer'], page_id, request_id,
                 supersedes, page['observed_at']))
            created.append(request_id)
        # Page receipt, every original observation and all task requests commit
        # together. Failure in any item leaves the original reliable cursor intact.
        if advance_cursor:
            c.execute('''INSERT INTO work_feedback_cursors VALUES (?,?,?,?,?,?)
                ON CONFLICT(task_id,source,review_id) DO UPDATE SET cursor_json=excluded.cursor_json,
                page_id=excluded.page_id,observed_at=excluded.observed_at''',
                (task_id, origin, page['review_id'], knowledge.canonical(page['next_cursor']), page_id, page['observed_at']))
    return {'status': 'ingested', 'page_id': page_id, 'created_requests': created, 'unchanged_requests': unchanged,
            'next_cursor': page['next_cursor'] if advance_cursor else None, 'collection_issues': collection_issues, 'task_state_changed': False,
            'history_coverage': 'Only the selected original pages are observed; earlier history and absent opinions remain unassessed.',
            'meaning': 'Observed original feedback only. The owner still decides scientific disposition and any authorized native action.'}


def ingest_capture(root, actor, payload):
    """Consume the teacher branch's exact native capture, never a guessed API.

    opinion_id is an internal canonical encoding of the native
    (review_id, reviewer_id) key, not an invented platform opinion UUID.
    """
    import review_adapter
    task_id = string(payload.get('task_id'), 'Original complete task')
    ref = core().evidence_ref(root, payload.get('capture_ref'))
    capture = review_adapter.load_capture(Path(ref['path']))
    if capture.get('profile') != 'native' and payload.get('offline') is not True:
        raise ValueError('Synthetic capture requires explicit offline=true and an isolated test database')
    expected_project = string(payload.get('expected_project'), 'Expected native project')
    account = string(payload.get('expected_account_user_id'), 'Expected current read account')
    observation = review_adapter.assess_capture(capture, expected_project=expected_project,
        expected_account_user_id=account)
    pages = []
    facts = {tuple(item['opinion_key']): item for item in observation['facts']['opinions']}
    for review in observation['facts']['reviews']:
        items = []
        for r, response in enumerate(capture['responses']):
            if response['rpc'] != 'qry_review_get_comment_items' or response['params'].get('p_review_id') != review['review_id']:
                continue
            for i, row in enumerate(response['data']):
                key = (row['review_id'], row['reviewer_id'])
                if key not in facts:
                    continue
                fact = facts[key]
                item = {'opinion_id': knowledge.canonical(list(key)),
                        'opinion_version': fact['modified_at'] + ':' + fact['observation_sha256'],
                        'content_sha256': fact['observation_sha256'], 'retracted': row['state_code'] == -2,
                        'pointer': '/responses/' + str(r) + '/data/' + str(i)}
                if not any(old['opinion_id'] == item['opinion_id'] for old in items):
                    items.append(item)
        page = {'review_id': review['review_id'], 'dataset_id': review['target'][1],
                'dataset_version': review['target'][2], 'observed_at': capture['source']['observed_at'],
                'cursor': None, 'next_cursor': None, 'items': items, 'source_ref': ref,
                'complete': observation['cursor'] is not None,
                'mapping': {'profile': capture['profile'],
                            'scope_sha256': hashlib.sha256(knowledge.canonical(capture['scope']).encode()).hexdigest(),
                            'opinion_identity': ['review_id', 'reviewer_id'],
                            'read': 'complete_native_snapshot' if observation['cursor'] else 'partial_native_snapshot'}}
        origin = expected_project if capture['profile'] == 'native' else 'offline-synthetic:' + expected_project
        pages.append(_ingest(root, actor, task_id, origin, page))
    return {'status': 'ingested', 'task_id': task_id, 'pages': pages,
            'profile': capture['profile'], 'collection': observation['cursor'],
            'evidence_gaps': [*observation['evidence_gaps'], *observation['blocked_conditions']],
            'native_action_performed': False}


def admit_maintenance(c, request_id):
    import harness_maintenance
    decision = harness_maintenance.state(c, request_id)
    if decision['state'] in ('closed', 'verified', 'duplicate', 'deferred'):
        core()._fail('maintenance_closed_or_deferred', 'Read the original explicit maintenance decision before reopening.', request_id)


def maintenance_is_disposed(c, request_id):
    import harness_maintenance
    decision = harness_maintenance.state(c, request_id)
    return decision['state'] in ('closed', 'verified', 'duplicate') and not harness_maintenance.new_observations(c, request_id, decision)


def validate_observation(c, root, request_id):
    """Validate the opinion itself, including its original byte and locator binding."""
    row = c.execute('SELECT * FROM work_feedback_observations WHERE request_id=?', (request_id,)).fetchone()
    if row is None:
        raise ValueError('Original feedback observation is missing')
    page = c.execute('SELECT * FROM work_feedback_pages WHERE id=?', (row['page_id'],)).fetchone()
    if page is None:
        raise ValueError('Original feedback page is missing')
    mapping = json.loads(page['mapping_json'])
    if mapping.get('read') == 'complete_native_snapshot':
        import review_adapter
        capture = knowledge.source(root, json.loads(row['source_ref_json']), structured=True)
        actual = review_adapter.assess_capture(capture)
        original = knowledge.pointer(capture, row['source_pointer'])
        fingerprint = review_adapter.digest(original)
        reviews = [item for item in actual.get('facts', {}).get('reviews', []) if item['review_id'] == row['review_id']]
        if (not actual.get('cursor') or actual['cursor']['scope_sha256'] != mapping['scope_sha256']
                or len(reviews) != 1 or tuple(reviews[0]['target'][1:]) != (row['dataset_id'], row['dataset_version'])
                or knowledge.canonical([original['review_id'], original['reviewer_id']]) != row['opinion_id']
                or fingerprint != row['content_sha256']
                or original['modified_at'] + ':' + fingerprint != row['opinion_version']
                or (original['state_code'] == -2) != bool(row['retracted'])):
            raise ValueError('Original native opinion identity, locator or content changed')
        return row
    parsed = parse_page(root, {'source_ref': json.loads(row['source_ref_json']), 'mapping': mapping})
    for key in ('review_id', 'dataset_id', 'dataset_version'):
        if parsed[key] != row[key]:
            raise ValueError('Original feedback identity changed: ' + key)
    items = [item for item in parsed['items'] if item['pointer'] == row['source_pointer']]
    if len(items) != 1 or any(items[0][key] != row[key] for key in
                             ('opinion_id', 'opinion_version', 'content_sha256', 'retracted')):
        raise ValueError('Original opinion locator or content changed')
    return row


def request_is_disposed(c, request_id):
    """Whether the owner recorded a response; this says nothing about data quality."""
    row = c.execute('SELECT decision FROM work_feedback_dispositions WHERE request_id=? ORDER BY sequence DESC LIMIT 1', (request_id,)).fetchone()
    return bool(row and row['decision'] != 'needs_clarification')


def dispose(root, actor, request_id, decision, note_path, *, revision_request_id=None):
    """Record the owner's explicit opinion response and ordinary original note."""
    if decision not in DISPOSITIONS:
        raise ValueError('Select an explicit opinion response.')
    ref, _ = core()._saved_document(root, note_path, allow_text=True)
    with core().transaction(root) as c:
        require_schema(c)
        core().verify_actor(root, actor)
        observation = c.execute('SELECT * FROM work_feedback_observations WHERE request_id=?', (request_id,)).fetchone()
        if observation is None:
            raise ValueError('Select the original feedback observation request.')
        core()._row(c, 'work_tasks', observation['task_id'])
        if revision_request_id:
            request = core()._row(c, 'work_requests', revision_request_id)
            if request['task_id'] != observation['task_id']:
                raise ValueError('The optional repair reference belongs to another task.')
        previous = c.execute('SELECT * FROM work_feedback_dispositions WHERE request_id=? ORDER BY sequence DESC LIMIT 1', (request_id,)).fetchone()
        values = (decision, knowledge.canonical(ref), knowledge.canonical(actor), revision_request_id)
        if previous and tuple(previous[k] for k in ('decision','evidence_ref_json','actor_json','revision_request_id')) == values:
            return dict(previous)
        ident = hashlib.sha256(knowledge.canonical([request_id, observation['id'], *values, previous['id'] if previous else None]).encode()).hexdigest()
        c.execute('INSERT INTO work_feedback_dispositions (id,request_id,observation_id,decision,evidence_ref_json,actor_json,supersedes_id,revision_request_id) VALUES (?,?,?,?,?,?,?,?)',
                  (ident,request_id,observation['id'],*values[:3],previous['id'] if previous else None,revision_request_id))
        return dict(c.execute('SELECT * FROM work_feedback_dispositions WHERE id=?',(ident,)).fetchone())


def maintenance_projection(root, c, row):
    import harness_maintenance
    ref = json.loads(row['source_ref_json'])
    return {'request_id':row['request_id'], 'source_ref':ref,
            'decision':harness_maintenance.state(c,row['request_id']),
            'integrity':knowledge.integrity(root,ref)}


def _related_maintenance(root, c, task_id, request_ids):
    """Project one original finding onto the exact affected request(s)."""
    related = {}
    selected = None if request_ids is None else set(request_ids)

    def add(finding_id, finding_task_id, affected, refs):
        affected = set(affected)
        if selected is not None:
            affected &= selected
        if not affected:
            return
        item = related.setdefault(finding_id, {
            'request_id': finding_id, 'finding_task_id': finding_task_id,
            'affected_request_ids': set(), 'observations': {},
            'original_finding_command': shlex.join(['python3', 'bin/context.py', '--request', finding_id]),
            'detail_command': shlex.join(['python3', 'bin/harness_feedback.py', 'show', '--task', task_id]),
        })
        item['affected_request_ids'].update(affected)
        for ref in refs:
            item['observations'].setdefault(knowledge.reference_key(root, ref), ref)

    for row in c.execute("""
        SELECT id, parent_request_id, input_ref_json FROM work_requests
        WHERE task_id=? AND kind='finding' AND recipient_role='maintainer' AND parent_request_id IS NOT NULL
        ORDER BY id
    """, (task_id,)):
        add(row['id'], task_id, [row['parent_request_id']], [json.loads(row['input_ref_json'])])

    for row in c.execute("""
        SELECT finding.id, finding.task_id, event.payload_json, returned.request_id AS source_request_id
        FROM work_task_events event
        JOIN work_requests finding ON finding.id = json_extract(event.payload_json, '$.request_id')
        LEFT JOIN work_returns returned ON returned.id = json_extract(event.payload_json, '$.return_id')
        LEFT JOIN work_requests original ON original.id = returned.request_id AND original.task_id = ?
        WHERE event.kind = 'maintenance-observation' AND event.task_id = finding.task_id
          AND finding.kind = 'finding' AND finding.recipient_role = 'maintainer'
          AND (json_extract(event.payload_json, '$.affected.task_id') = ?
               OR (json_type(event.payload_json, '$.affected') IS NULL AND original.id IS NOT NULL))
        ORDER BY event.id DESC
    """, (task_id, task_id)):
        event = json.loads(row['payload_json'])
        affected = event['affected']['request_ids'] if 'affected' in event else [row['source_request_id']]
        add(row['id'], row['task_id'], affected, [event['result_ref'], *event.get('occurrence_refs', [])])

    for finding_id, item in related.items():
        item['affected_request_ids'] = sorted(item['affected_request_ids'])
        item['observations'] = [{'source_ref': ref, 'integrity': knowledge.integrity(root, ref)}
                                for ref in item['observations'].values()]
        import harness_maintenance
        item['decision'] = harness_maintenance.state(c,finding_id)
        item['new_observations'] = harness_maintenance.new_observations(c,finding_id,item['decision'])
        item['returns'] = [core()._decode(r) for r in c.execute(
            'SELECT * FROM work_returns WHERE request_id=? ORDER BY created_at,id',(finding_id,))]
        link = c.execute('SELECT * FROM work_maintenance_links WHERE request_id=?',(finding_id,)).fetchone()
        if link:
            item['historical_diagnosis'] = maintenance_projection(root,c,link)
    return list(related.values())


def related_maintenance(root, task_id, *, request_ids=None):
    with knowledge.read_connection(root) as c:
        if not knowledge.has_table(c, 'work_maintenance_links'):
            return []
        return _related_maintenance(root, c, task_id, request_ids)


def current_view(root, task_id, *, request_id=None):
    snapshot = core().read_task(root, task_id)
    with knowledge.read_connection(root) as c:
        require_schema(c)
        if request_id is not None:
            rows = c.execute('SELECT * FROM work_feedback_observations WHERE task_id=? AND request_id=? ORDER BY sequence',
                             (task_id, request_id)).fetchall()
        else:
            rows = c.execute('''SELECT a.* FROM work_feedback_observations a WHERE a.task_id=?
                AND NOT EXISTS (SELECT 1 FROM work_feedback_observations b WHERE
                  b.task_id=a.task_id AND b.source=a.source AND b.review_id=a.review_id AND b.dataset_id=a.dataset_id
                  AND b.opinion_id=a.opinion_id AND (julianday(b.observed_at)>julianday(a.observed_at)
                    OR julianday(b.observed_at)=julianday(a.observed_at) AND b.sequence>a.sequence)) ORDER BY a.sequence''', (task_id,)).fetchall()
        observations = []
        requests = {r['id']: r for r in snapshot.get('requests', [])}
        superseded = {r.get('supersedes_request_id') for r in requests.values()}
        for row in rows:
            item = {k: row[k] for k in ('id', 'source', 'review_id', 'dataset_id', 'dataset_version',
                    'opinion_id', 'opinion_version', 'content_sha256', 'source_pointer', 'request_id',
                    'supersedes_request_id', 'observed_at')}
            item['retracted'] = bool(row['retracted'])
            item['source_ref'] = json.loads(row['source_ref_json'])
            item['integrity'] = knowledge.integrity(root, item['source_ref'])
            request = requests.get(row['request_id'])
            item['request_state'] = ('unavailable' if request is None else 'superseded' if row['request_id'] in superseded
                                     else 'pending' if row['request_id'] in snapshot.get('pending_request_ids', []) else 'disposed')
            disposition = c.execute('SELECT * FROM work_feedback_dispositions WHERE request_id=? ORDER BY sequence DESC LIMIT 1', (row['request_id'],)).fetchone()
            if disposition:
                item['disposition'] = {'decision': disposition['decision'],
                                       'evidence_ref': json.loads(disposition['evidence_ref_json']),
                                       'revision_request_id': disposition['revision_request_id'],
                                       'response_recorded': True,
                                       'integrity': knowledge.integrity(root, json.loads(disposition['evidence_ref_json']))}
            observations.append(item)
        maintenance = {r['request_id']: maintenance_projection(root, c, r) for r in c.execute(
            'SELECT * FROM work_maintenance_links WHERE task_id=? ORDER BY request_id', (task_id,))}
        maintenance.update((item['request_id'], item) for item in _related_maintenance(root, c, task_id, None))
        cursors = [dict(r) for r in c.execute(
            'SELECT source,review_id,cursor_json,observed_at FROM work_feedback_cursors WHERE task_id=?', (task_id,))]
        for cursor in cursors:
            cursor['next_cursor'] = json.loads(cursor.pop('cursor_json'))
    return {'schema': 'data-workspace.task-feedback.v1', 'task_id': task_id,
            'observations': observations, 'reliable_cursors': cursors, 'maintenance': list(maintenance.values()),
            'history_coverage': 'Selected original pages only. Pre-import history and opinions absent from a page remain unassessed; absence is never inferred as retraction.',
            'collector': {'status': 'unavailable_live_collector', 'entry': 'python3 bin/harness_feedback.py ingest-capture --payload FILE',
                          'reason': 'No verified complete review/opinion collector is configured here. Import only actual original receipts from the supported existing tool, with an explicit field mapping.'},
            'disposition_semantics': list(DISPOSITIONS),
            'authority': 'Observations and engineering links neither change native dataset/task state nor authorize replies, revision, publication or scientific adoption.'}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=ROOT)
    sub = parser.add_subparsers(dest='command', required=True)
    q = sub.add_parser('show'); q.add_argument('--task', required=True); q.add_argument('--request')
    for name in ('ingest-page','ingest-capture'):
        q = sub.add_parser(name); q.add_argument('--payload', type=Path, required=True)
    q = sub.add_parser('dispose')
    q.add_argument('--request',required=True); q.add_argument('--decision',choices=DISPOSITIONS,required=True)
    q.add_argument('--note',required=True); q.add_argument('--revision-request')
    args = parser.parse_args(argv)
    try:
        if args.command == 'show':
            value = current_view(args.root,args.task,request_id=args.request)
        else:
            actor = core().verified_actor(args.root)
            if args.command == 'dispose':
                value = dispose(args.root,actor,args.request,args.decision,args.note,revision_request_id=args.revision_request)
            else:
                payload=json.loads(args.payload.read_bytes())
                value=(ingest_page if args.command=='ingest-page' else ingest_capture)(args.root,actor,payload)
        print(json.dumps(value,ensure_ascii=False,indent=2)); return 0
    except (ValueError,OSError,KeyError,sqlite3.Error) as exc:
        print(json.dumps({'status':'unavailable','error':str(exc)})); return 1


if __name__ == '__main__':
    raise SystemExit(main())
