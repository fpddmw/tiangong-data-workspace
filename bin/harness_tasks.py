#!/usr/bin/env python3
"""Complete-task responsibility in the existing harness database.

These are local coordination facts, not dataset state, platform authority or
professional qualification. Reads never initialize or migrate the database.
"""

import argparse
import contextlib
import datetime
import hashlib
import importlib
import json
import os
from pathlib import Path
import re
import shlex
import sqlite3
import sys

from task_ledger import finite_work as finite
from task_ledger import store
from harness_hosts import supports

VERSION = 3
ORG_ROLES = ('global_main', 'task_lead', 'executor', 'maintainer')
REQUEST_KINDS = ('plan', 'review', 'finding', 'feedback')
RUN_OUTCOMES = ('intent', 'unknown', 'running', 'succeeded', 'failed', 'stopped')


def pending_runs(c, request_id):
    """Uncontinued native attempts; earlier unknown observations stay in history."""
    return c.execute("""SELECT r.* FROM work_runs r WHERE r.request_id=?
        AND r.outcome IN ('intent','unknown','running') AND NOT EXISTS (
            SELECT 1 FROM work_run_events e WHERE e.kind='intent'
            AND json_extract(e.payload_json,'$.previous_run_id')=r.id)
        ORDER BY r.created_at,r.id""", (request_id,)).fetchall()


def session_origin(c, run):
    """Follow recorded resume intents to an observed Session, never invent one."""
    seen = set()
    request_id, host = run['request_id'], run['target_host']
    while run['id'] not in seen:
        seen.add(run['id'])
        if run['request_id'] != request_id or run['target_host'] != host:
            break
        if run['native_identity_json']:
            return run
        if run['operation'] != 'resume':
            break
        event = c.execute("SELECT payload_json FROM work_run_events WHERE run_id=? AND kind='intent' ORDER BY id LIMIT 1",
                          (run['id'],)).fetchone()
        previous = json.loads(event['payload_json']).get('previous_run_id') if event else None
        if not previous:
            break
        run = _row(c, 'work_runs', previous)
    _fail('native_identity_missing', 'The original attempt has no observed Session to continue.', run['id'])


class WorkConflict(ValueError):
    def __init__(self, code, message, record_id=None):
        super().__init__(message)
        self.code, self.record_id = code, record_id


def _fail(code, message, record_id=None):
    raise WorkConflict(code, message, record_id)


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def _now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _text(value, name):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(name + ' must be a nonempty string')
    return value


def _list(value, name, nonempty=False):
    if not isinstance(value, list) or (nonempty and not value):
        raise ValueError(name + ' must be an explicit list')
    if any(not isinstance(x, str) or not x.strip() for x in value) or len(set(value)) != len(value):
        raise ValueError(name + ' needs distinct nonempty strings')
    return value


def actor_key(actor):
    """Serialize recorded actor provenance; this is not an authorization key."""
    return _json({k: actor[k] for k in ('host', 'kind', 'id', 'instance', 'cwd')})


def native_key(actor):
    """Actual host context, independent of where its original work was saved."""
    if isinstance(actor, str):
        actor = json.loads(actor)
    return tuple(actor[k] for k in ('host', 'kind', 'id'))


def workspace_root(c):
    """Locate the open store itself, never a historical actor's working directory."""
    return Path(next(row[2] for row in c.execute('PRAGMA database_list') if row[1] == 'main')).parent.parent


def _actor(root, actor, observed=False):
    from harness_hosts import is_observed_actor
    if not isinstance(actor, dict):
        raise ValueError('An exact actor identity is required')
    base = {k: actor.get(k) for k in ('host', 'kind', 'id', 'instance', 'cwd')}
    for key, value in base.items():
        _text(value, 'Actor ' + key)
    if not is_observed_actor(base['host'], base['kind']) and (base['host'], base['kind']) != ('local', 'process'):
        _fail('unsupported_identity', 'This host identity has no known native identity source')
    if not Path(base['cwd']).is_absolute():
        raise ValueError('Record an absolute working-directory provenance')
    if 'turn_id' in actor:
        base['turn_id'] = _text(actor['turn_id'], 'Native turn id')
    return base


def verified_actor(root, actor=None):
    """Observe the caller for attribution, never infer organizational authority."""
    from harness_hosts import observed_shell_identity
    hint = observed_shell_identity(os.environ)
    if thread := os.environ.get('CODEX_THREAD_ID'):
        host, kind, native_id = 'codex', 'thread', thread
    elif hint:
        host, kind, native_id = hint
    elif session := os.environ.get('CLAUDE_SESSION_ID'):
        host, kind, native_id = 'claude_code', 'session', session
    else:
        host, kind, native_id = 'local', 'process', str(os.getpid())
    actual = {'host': host, 'kind': kind, 'id': native_id,
              'instance': native_id, 'cwd': str(Path(root).resolve())}
    if actor is not None and native_key(_actor(root, actor)) != native_key(actual):
        _fail('host_identity_mismatch', 'Supplied actor does not match the observed caller')
    return actual


def observed_request_actor(root, request_id):
    """Locate an already-observed exact request for read-only context."""
    from harness_hosts import observed_shell_identity

    if not request_id or not (hint := observed_shell_identity(os.environ)):
        return None
    host, kind, session_id = hint
    with contextlib.closing(_connect(root, True)) as c:
        row = c.execute("""SELECT native_identity_json FROM work_runs
            WHERE request_id=? AND target_host=?
            AND json_extract(native_identity_json,'$.kind')=?
            AND json_extract(native_identity_json,'$.id')=?
            ORDER BY created_at DESC,id DESC LIMIT 1""", (request_id, host, kind, session_id)).fetchone()
    return _actor(root, json.loads(row[0]), observed=True) if row else None


# Explicit spelling used by the knowledge/feedback adapters.
verify_actor = verified_actor


def evidence_ref(root, ref):
    """Verify original saved bytes through the existing credential/locator guard."""
    if not isinstance(ref, dict):
        raise ValueError('Evidence requires an exact path and sha256')
    path = store.evidence_path(Path(root).resolve(), ref)
    result = {'path': path, 'sha256': ref['sha256']}
    if 'version' in ref:
        result['version'] = _text(ref['version'], 'Evidence version')
    return result


def _plan_ref(root, ref):
    from production_review import _save_fixed, scientific_path
    verified=evidence_ref(root,ref)
    logical=Path(root)/ref['path']
    path=scientific_path(root,logical,must_exist=False)
    archive=store.plan_history_path(path,verified['sha256'])
    raw=Path(verified['path']).read_bytes()
    if hashlib.sha256(raw).hexdigest()!=verified['sha256']:
        raise ValueError('Plan changed while recording its revision.')
    _save_fixed(root,archive,raw)
    return {**verified,'path':str(path)}


def _freeze_plan(c, root, task, previous=None, reason=None):
    from harness_scope import contract, _scope_rows
    from task_ledger.decisions import resolve
    decisions = resolve(root, {'task': task['id']}, connection=c, verify_sources=False)['subjects']
    return {'version': task['plan_revision'], 'plan_ref': json.loads(task['plan_ref_json']),
            'previous_plan_ref': previous, 'reason_ref': reason,
            'contract': contract(c, 'task', task['id']), 'scope': _scope_rows(c, task),
            'decision_refs': [{'id': d['id'], 'source_ref': d['source_ref']}
                              for subject in decisions if subject['status'] == 'applicable'
                              for d in subject['decisions']]}


def _fresh_request(c, task, request):
    """Apply explicit baseline changes at first send, preserving original attempts."""
    if request['kind'] != 'plan' or request['independent'] or request['plan_revision'] == task['plan_revision']:
        return
    if c.execute("SELECT 1 FROM work_runs WHERE request_id=? AND (native_identity_json IS NOT NULL OR outcome='unknown')",
                 (request['id'],)).fetchone():
        return
    scope = set(json.loads(request['required_scope_json']))
    for row in c.execute("""SELECT payload_json FROM work_task_events WHERE task_id=? AND kind='revise'
            AND plan_revision>? AND json_type(payload_json,'$.baseline')='object' ORDER BY id""",
                         (task['id'], request['plan_revision'])):
        update = json.loads(row[0])
        if (request['id'] in update.get('affected_request_ids', ())
                or scope.intersection(update['affected_scope'])
                or update.get('affects_all_unsent')):
            _fail('request_baseline_changed', 'Recheck this unsent assignment against the current frozen commitment.', request['id'])


def check_inputs(root, refs):
    issues = []
    for ref in refs:
        try:
            evidence_ref(root, ref)
        except (OSError, ValueError) as exc:
            issues.append({'source_ref': ref, 'reason': str(exc)})
    return issues


def _refs(root, refs, name, nonempty=False):
    if not isinstance(refs, list) or (nonempty and not refs):
        raise ValueError(name + ' requires an explicit list of original references')
    return [evidence_ref(root, ref) for ref in refs]


def _exists(c, table):
    return c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone() is not None


def ensure_schema(c):
    if not _exists(c, 'work_contract'):
        _fail('work_unconfigured', 'Complete-task contract is not installed; explicit work-init is required')
    row = c.execute('SELECT version FROM work_contract WHERE singleton=1').fetchone()
    if row is None or row['version'] != VERSION:
        _fail('unsupported_work_contract', 'Unsupported complete-task contract; preserve the original database')


def add_schema(c, statements, table_names):
    """Explicit initialization helper for cohesive same-database extensions."""
    for statement in statements:
        c.execute(statement)
    for table in table_names:
        if not re.fullmatch(r'work_[a-z_]+', table):
            raise ValueError('Only named work_* extension tables may be guarded')
        for operation in ('INSERT', 'UPDATE', 'DELETE'):
            c.execute(f'''CREATE TRIGGER IF NOT EXISTS {table}_writer_{operation.lower()}
                BEFORE {operation} ON {table}
                WHEN work_contract_writer() != 3
                BEGIN SELECT RAISE(ABORT,'complete-task writer upgrade required'); END''')


SCHEMA = [
    '''CREATE TABLE work_contract (singleton INTEGER PRIMARY KEY CHECK(singleton=1),
       version INTEGER NOT NULL, migration_id TEXT NOT NULL UNIQUE, evidence_ref_json TEXT NOT NULL,
       initialized_by TEXT NOT NULL, initialized_at TEXT NOT NULL)''',
    '''CREATE TABLE work_roles (actor_key TEXT NOT NULL, org_role TEXT NOT NULL,
       actor_json TEXT NOT NULL, granted_by TEXT NOT NULL, evidence_ref_json TEXT NOT NULL,
       created_at TEXT NOT NULL, PRIMARY KEY(actor_key,org_role))''',
    '''CREATE TABLE work_tasks (id TEXT PRIMARY KEY, original_ref_json TEXT NOT NULL,
       plan_ref_json TEXT NOT NULL, owner_actor TEXT NOT NULL, sponsor_actor TEXT NOT NULL,
       summary TEXT NOT NULL, required_scope_json TEXT NOT NULL, revision INTEGER NOT NULL,
       plan_revision INTEGER NOT NULL, state TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)''',
    '''CREATE TABLE work_task_events (id INTEGER PRIMARY KEY, task_id TEXT NOT NULL REFERENCES work_tasks(id),
       kind TEXT NOT NULL, actor_json TEXT NOT NULL, revision INTEGER NOT NULL, plan_revision INTEGER NOT NULL,
       payload_json TEXT NOT NULL, created_at TEXT NOT NULL)''',
    '''CREATE TABLE work_requests (id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES work_tasks(id),
       kind TEXT NOT NULL, sender_actor TEXT NOT NULL, recipient_actor TEXT, recipient_role TEXT NOT NULL,
       input_ref_json TEXT NOT NULL, summary TEXT NOT NULL, plan_revision INTEGER NOT NULL,
       required_scope_json TEXT NOT NULL, independent INTEGER NOT NULL, requires_review INTEGER NOT NULL,
       authors_json TEXT NOT NULL, parent_request_id TEXT REFERENCES work_requests(id),
       supersedes_request_id TEXT REFERENCES work_requests(id), created_at TEXT NOT NULL)''',
    '''CREATE TABLE work_returns (id TEXT PRIMARY KEY, request_id TEXT NOT NULL REFERENCES work_requests(id),
       run_id TEXT, plan_revision INTEGER NOT NULL, author_actor TEXT NOT NULL, identity_assurance TEXT NOT NULL,
       result_ref_json TEXT NOT NULL, artifact_refs_json TEXT NOT NULL, coverage_json TEXT NOT NULL,
       remaining_scope_json TEXT NOT NULL, summary TEXT NOT NULL, registered_by TEXT NOT NULL,
       native_receipt_json TEXT, created_at TEXT NOT NULL)''',
    '''CREATE TABLE work_consumptions (id TEXT PRIMARY KEY, return_id TEXT NOT NULL REFERENCES work_returns(id),
       actor_json TEXT NOT NULL, evidence_ref_json TEXT NOT NULL, summary TEXT NOT NULL, created_at TEXT NOT NULL)''',
    '''CREATE TABLE work_dispositions (id TEXT PRIMARY KEY, return_id TEXT NOT NULL REFERENCES work_returns(id),
       actor_json TEXT NOT NULL, disposition TEXT NOT NULL, scope_json TEXT NOT NULL,
       remaining_scope_json TEXT NOT NULL, responsible_actor TEXT NOT NULL, summary TEXT NOT NULL,
       evidence_ref_json TEXT NOT NULL, review_return_ids_json TEXT NOT NULL, conditions_json TEXT NOT NULL,
       supersedes_json TEXT NOT NULL, plan_revision INTEGER NOT NULL, compatibility_ref_json TEXT,
       created_at TEXT NOT NULL)''',
    '''CREATE TABLE work_runs (id TEXT PRIMARY KEY, request_id TEXT NOT NULL REFERENCES work_requests(id),
       dispatcher_actor TEXT NOT NULL, plan_revision INTEGER NOT NULL, task_revision INTEGER NOT NULL,
       config_ref_json TEXT NOT NULL, input_refs_json TEXT NOT NULL, target_host TEXT NOT NULL,
       operation TEXT NOT NULL, outcome TEXT NOT NULL, native_identity_json TEXT,
       stop_requested INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)''',
    '''CREATE TABLE work_run_events (id INTEGER PRIMARY KEY, run_id TEXT NOT NULL REFERENCES work_runs(id),
       kind TEXT NOT NULL, actor_json TEXT NOT NULL, receipt_json TEXT, payload_json TEXT NOT NULL,
       created_at TEXT NOT NULL)''',
]
TABLES = ('work_contract', 'work_roles', 'work_tasks', 'work_task_events', 'work_requests',
          'work_returns', 'work_consumptions', 'work_dispositions', 'work_runs', 'work_run_events')


def _connect(root, readonly=False, *, timeout=30):
    root = Path(root).resolve()
    path = root / '.control' / 'harness.sqlite'
    if not path.is_file():
        _fail('work_unconfigured', 'Harness database is missing; reads and ordinary mutations cannot initialize it')
    c = sqlite3.connect(path.as_uri() + ('?mode=ro' if readonly else '?mode=rw'),
                        uri=True, timeout=timeout, isolation_level=None)
    c.row_factory = sqlite3.Row
    c.execute('PRAGMA foreign_keys=ON')
    if readonly:
        c.execute('PRAGMA query_only=ON')
    else:
        c.create_function('work_contract_writer', 0, lambda: VERSION)
    return c


@contextlib.contextmanager
def transaction(root, *, timeout=30):
    """Atomic writes to the same SQLite; never bootstraps an absent contract."""
    c = _connect(root, timeout=timeout)
    try:
        c.execute('BEGIN IMMEDIATE')
        ensure_schema(c)
        with store.evidence_connection(c):
            yield c
        c.execute('COMMIT')
    except BaseException:
        if c.in_transaction:
            c.execute('ROLLBACK')
        raise
    finally:
        c.close()


def write_contention(error):
    return (isinstance(error, sqlite3.OperationalError)
            and (getattr(error, 'sqlite_errorcode', 0) & 0xff) in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED))


def install_contract(c, actor, evidence, idempotency_key):
    """Install on a caller-owned transaction, including isolated migration copies."""
    if _exists(c, 'work_contract'):
        ensure_schema(c)
        row = c.execute('SELECT * FROM work_contract WHERE singleton=1').fetchone()
        if row['migration_id'] != idempotency_key or row['evidence_ref_json'] != _json(evidence) or row['initialized_by'] != actor_key(actor):
            _fail('migration_conflict', 'Installation retry must retain its original actor, evidence and migration id')
    else:
        add_schema(c, SCHEMA, TABLES)
        c.execute('INSERT INTO work_contract VALUES (1,?,?,?,?,?)',
                  (VERSION, idempotency_key, _json(evidence), actor_key(actor), _now()))
        c.execute('INSERT INTO work_roles VALUES (?,?,?,?,?,?)',
                  (actor_key(actor), 'global_main', _json(actor), actor_key(actor), _json(evidence), _now()))
    # Extensions add references and observations to this same transaction.
    for name in ('harness_knowledge', 'harness_feedback', 'task_ledger.business', 'harness_scope'):
        try:
            extension = importlib.import_module(name)
        except ModuleNotFoundError as exc:
            if exc.name != name:
                raise
            continue
        extension.install_schema(c)


def initialize(root, actor, evidence, idempotency_key):
    """Explicit additive installation; no production admission is implied."""
    root, actor = Path(root).resolve(), verified_actor(root, actor)
    evidence = evidence_ref(root, evidence)
    _text(idempotency_key, 'Migration id')
    # This is the only complete-task API allowed to create the existing database.
    c = store.initialize_database(root)
    c.create_function('work_contract_writer', 0, lambda: VERSION)
    try:
        c.execute('BEGIN IMMEDIATE')
        install_contract(c, actor, evidence, idempotency_key)
        c.execute('COMMIT')
        return {'status': 'installed', 'version': VERSION, 'migration_id': idempotency_key,
                'database': str(root / '.control' / 'harness.sqlite'), 'production_admission': False}
    except BaseException:
        if c.in_transaction:
            c.execute('ROLLBACK')
        raise
    finally:
        c.close()


def _row(c, table, ident):
    row = c.execute(f'SELECT * FROM {table} WHERE id=?', (ident,)).fetchone()
    if row is None:
        _fail('not_found', 'Unknown ' + table + ' record', ident)
    return row


def _roles(c, actor):
    return {row['org_role'] for row in c.execute('SELECT * FROM work_roles')
            if native_key(row['actor_json']) == native_key(actor)}


def _revision(task, payload, required=True):
    expected = payload.get('expected_task_revision')
    if expected is None and not required:
        return
    if not isinstance(expected, int) or isinstance(expected, bool) or expected != task['revision']:
        _fail('task_revision_conflict', 'Task changed; reread its current responsibility and plan before writing', task['id'])


def _active(task):
    if task['state'] != 'active':
        _fail('task_not_active', 'Current task is ' + task['state'] + '; dispatch and new effects remain gated', task['id'])


def _scope(task, values, name, nonempty=True):
    values = _list(values, name, nonempty)
    return values


def _admit_scope(c, task):
    if task['state'] != 'active':
        _fail('task_not_active', 'Current task is ' + task['state'] + '; launch/resume remains gated', task['id'])


def _admit_request(c, task, req, *, side_effect=False):
    """Shared maintenance may outlive scientific completion on its own finding.

    The finding must be explicitly assigned to maintenance. A scientific pause
    leaves that repair available; cancellation and native-run guards still apply.
    This changes neither task state nor scientific adoption.
    """
    from task_ledger import maintenance as harness_maintenance
    if side_effect and _delegation_decisions(c, task['id']).get(req['id'], {}).get('action') == 'end':
        _fail('delegation_ended', 'This assignment was ended. Its owner can explicitly continue the original Session with --instructions and --resume-stopped after reading the original effects.', req['id'])
    parent = harness_maintenance.parent_finding(c, req)
    shared = (req['kind'] == 'finding' and req['recipient_role'] == 'maintainer') or parent is not None
    if shared and task['state'] in ('active', 'completed', 'paused'):
        root = workspace_root(c)
        evidence_ref(root, json.loads(req['input_ref_json']))
        import harness_feedback
        harness_feedback.admit_maintenance(c, parent['id'] if parent is not None else req['id'])
        return 'shared_maintenance'
    _admit_scope(c, task)
    if side_effect:
        _fresh_request(c, task, req)
        if req['kind']=='plan' and not req['independent']:
            from harness_wave import guard_reserve
            guard_reserve(c,workspace_root(c),task,req)
    if side_effect and req['independent']:
        from scientific_review.queue import guard
        guard(c,workspace_root(c),_decode(req))
    if side_effect and req['kind'] == 'plan' and not req['independent']:
        finite_configs = finite.request_configs(c, task['id'])
        original = req['parent_request_id'] if req['independent'] else req['id']
        if finite.successor(finite_configs, original):
            _fail('package_already_continued', 'Use the explicitly continued original package request', original)
    return 'scientific_task'


def participates(c, task, actor):
    """Optional personal-view filter; participation never grants tool authority."""
    key = native_key(actor)
    actors = [task['owner_actor'], task['sponsor_actor']]
    for row in c.execute('SELECT sender_actor,recipient_actor FROM work_requests WHERE task_id=?', (task['id'],)):
        actors.extend(value for value in row if value)
    actors.extend(row[0] for row in c.execute('''SELECT r.native_identity_json FROM work_runs r
        JOIN work_requests q ON q.id=r.request_id WHERE q.task_id=? AND r.native_identity_json IS NOT NULL''', (task['id'],)))
    return any(native_key(value) == key for value in actors)


def _event(c, task, kind, actor, payload):
    c.execute('INSERT INTO work_task_events(task_id,kind,actor_json,revision,plan_revision,payload_json,created_at) VALUES (?,?,?,?,?,?,?)',
              (task['id'], kind, _json(actor), task['revision'], task['plan_revision'], _json(payload), _now()))


def _touch(c, task, **updates):
    fields = {'revision': task['revision'] + 1, 'updated_at': _now(), **updates}
    c.execute('UPDATE work_tasks SET ' + ','.join(k + '=?' for k in fields) + ' WHERE id=? AND revision=?',
              (*fields.values(), task['id'], task['revision']))
    return _row(c, 'work_tasks', task['id'])


def _immutable(c, table, ident, fields):
    row = c.execute(f'SELECT * FROM {table} WHERE id=?', (ident,)).fetchone()
    if row is not None:
        if any(row[key] != value for key, value in fields.items()):
            _fail('idempotency_conflict', 'Retry id must retain the exact original record; changed bytes require a new version', ident)
        return row
    columns = ['id', *fields, 'created_at']
    c.execute(f'INSERT INTO {table} ({",".join(columns)}) VALUES ({",".join("?" for _ in columns)})',
              (ident, *fields.values(), _now()))
    return _row(c, table, ident)


def _decode(row):
    result = dict(row)
    for key in list(result):
        if key.endswith('_json'):
            result[key[:-5]] = json.loads(result.pop(key)) if result[key] is not None else None
    for key in ('owner_actor', 'sponsor_actor', 'sender_actor', 'recipient_actor', 'author_actor', 'registered_by',
                'responsible_actor', 'dispatcher_actor', 'actor_json'):
        value = result.get(key)
        if isinstance(value, str) and value.startswith('{'):
            result[key] = json.loads(value)
    if 'recipient_role' in result:
        result['recipient'] = result.get('recipient_actor')
    return result


def request_authors(c, request):
    """Read known producing contexts from original runs and returns."""
    contexts = {}
    for row in c.execute('SELECT native_identity_json FROM work_runs WHERE request_id=? AND native_identity_json IS NOT NULL', (request['id'],)):
        actor = json.loads(row[0]); contexts[actor_key(actor)] = actor
    for row in c.execute('SELECT author_actor FROM work_returns WHERE request_id=?', (request['id'],)):
        actor = json.loads(row[0]); contexts[actor_key(actor)] = actor
    return list(contexts.values())


def _independent(req, task, candidate):
    if not req['independent']:
        return
    authors = [native_key(a) for a in json.loads(req['authors_json'])]
    if native_key(candidate) in authors:
        _fail('reviewer_is_author', 'Independent review needs a separate actual native author context', req['id'])


def _native_receipt(root, ref, run, native=None):
    ref = evidence_ref(root, ref)
    doc = json.loads(Path(ref['path']).read_text())
    if not isinstance(doc, dict) or doc.get('schema') != 'data-workspace.agent-runtime-observation.v1':
        _fail('native_receipt_required', 'Runtime binding requires the adapter\'s original native observation receipt', run['id'])
    # The source records how the adapter observed this run. Historical
    # observations need not use the selected adapter's current transport.
    if doc.get('source') not in ('native_rpc', 'native_cli_json') or doc.get('host') != run['target_host']:
        _fail('native_receipt_conflict', 'Receipt is not a native observation of this run host', run['id'])
    if doc.get('config_sha256') != json.loads(run['config_ref_json'])['sha256']:
        _fail('native_config_conflict', 'Native observation must bind the exact selected configuration bytes', run['id'])
    if doc.get('run_id') != run['id'] or doc.get('request_id') != run['request_id']:
        _fail('native_run_conflict', 'Native observation must retain the exact original run and request ids', run['id'])
    _text(doc.get('observed_at'), 'Native observation time')
    identity = _actor(root, doc.get('native_identity'), observed=True)
    if native is not None and _json(identity) != _json(_actor(root, native, observed=True)):
        _fail('native_identity_conflict', 'Supplied native identity differs from the adapter observation', run['id'])
    if identity['host'] != run['target_host']:
        _fail('native_identity_conflict', 'Observed native host differs from the run intent', run['id'])
    return ref, doc, identity


def _run_event(c, run, kind, actor, receipt, payload, *, observed_at=None):
    c.execute('INSERT INTO work_run_events(run_id,kind,actor_json,receipt_json,payload_json,created_at) VALUES (?,?,?,?,?,?)',
              (run['id'], kind, _json(actor), _json(receipt) if receipt else None, _json(payload), observed_at or _now()))


def _record_capacity(c, root, actor, payload, *, observed_at=None):
    from dsh_capacity import POOL_NAMES, _paths

    ident = _text(payload.get('id'), 'Original capacity run')
    run = _row(c, 'work_runs', ident)
    if (run['target_host'] not in POOL_NAMES
            or payload.get('queue_source') != str(_paths(root, run['target_host'])[0])):
        raise ValueError('Capacity observation must come from the original run host queue')
    phase = payload.get('phase')
    if phase not in ('queued','admitted','released','abandoned','cancelled_before_start',
                     'waiting_review','waiting_retry','transport_handoff'):
        raise ValueError('Unknown technical capacity observation')
    if payload.get('kind') not in ('author','review') or not isinstance(payload.get('sequence'), int):
        raise ValueError('Capacity observation must retain its class and FIFO sequence')
    event = {k:payload[k] for k in ('phase','kind','sequence','queue_source')}
    result = {'run_id':ident,'capacity_phase':phase,'business_outcome_unchanged':True}
    if payload.get('reason') in ('review_wait_execution_exited','model_retry_execution_exited') and phase=='abandoned':
        event['reason'] = payload['reason']
        if payload.get('authority_ref'):
            event['authority_ref'] = evidence_ref(root,payload['authority_ref'])
        if c.execute('''SELECT 1 FROM work_run_events WHERE run_id=? AND kind='capacity'
                AND json_extract(payload_json, '$.sequence')=? AND json_extract(payload_json, '$.reason')=?''',
                (ident,event['sequence'],event['reason'])).fetchone():
            return result
    if not observed_at or not c.execute('''SELECT 1 FROM work_run_events WHERE run_id=? AND kind='capacity'
            AND created_at=? AND payload_json=?''',(ident,observed_at,_json(event))).fetchone():
        _run_event(c,run,'capacity',actor,None,event,observed_at=observed_at)
    return result


def recovery_admission(root, run_id, actor):
    """Expose the exact original run without a chat-owner recovery protocol."""
    with contextlib.closing(_connect(root, True)) as c:
        _row(c, 'work_runs', run_id)
    return {'mode': 'original_run', 'allowed': ['read_original', 'record_original_outcome', 'stop_original'],
            'may_launch': False}


@contextlib.contextmanager
def recovery_boundary(root, run_id, actor, native):
    """Order each short native read/interrupt with actual ownership and run changes."""
    actor = verified_actor(root, actor)
    with transaction(root) as c:
        run = _row(c, 'work_runs', run_id)
        if json.loads(run['native_identity_json']) != native:
            _fail('recovery_identity_changed', 'Recover only the original observed native identity', run_id)
        yield


def _artifact_refs(root, refs, nonempty=False):
    if not isinstance(refs, list):
        raise ValueError('Artifact references must be a list of saved versions')
    result, ids = [], set()
    for item in refs:
        if not isinstance(item, dict):
            raise ValueError('Artifact version requires id, version, path and sha256')
        ident, version = _text(item.get('id'), 'Artifact id'), _text(item.get('version'), 'Artifact version')
        if ident in ids:
            raise ValueError('An artifact identity may appear only once in a return version')
        ids.add(ident)
        result.append({'id': ident, **evidence_ref(root, item), 'version': version})
    return result


def _effective_dispositions(c, task_id, *, return_id=None, before=None, request_ids=None, scope_ids=()):
    if return_id is not None:
        rows = c.execute('SELECT * FROM work_dispositions WHERE return_id=? ORDER BY created_at,id',
                         (return_id,)).fetchall()
    else:
        filter_sql, args = '', (task_id,)
        if request_ids is not None:
            filter_sql = '''AND (r.request_id IN (SELECT value FROM json_each(?)) OR EXISTS (
                SELECT 1 FROM work_dispositions candidate JOIN json_each(candidate.scope_json) s
                WHERE candidate.return_id=r.id AND s.value IN (SELECT value FROM json_each(?))))'''
            args += (_json(list(request_ids)), _json(list(scope_ids)))
        rows = c.execute('''SELECT d.* FROM work_dispositions d JOIN work_returns r ON r.id=d.return_id
                         JOIN work_requests q ON q.id=r.request_id WHERE q.task_id=? '''
                         + filter_sql + ' ORDER BY d.created_at,d.id', args).fetchall()
    if before is not None:
        rows = [row for row in rows if row['created_at'] <= before]
    superseded = {ident for row in rows for ident in json.loads(row['supersedes_json'])}
    return [row for row in rows if row['id'] not in superseded]


def _delegation_decisions(c, task_id):
    """Read explicit action events; scientific reports never act as stop protocols."""
    decisions = {}
    for row in c.execute("SELECT payload_json FROM work_task_events WHERE task_id=? AND kind='delegation-decision' ORDER BY id", (task_id,)):
        event = json.loads(row[0])
        if event.get('action') in ('end', 'resume'):
            decisions[event['request_id']] = event
    return decisions


def delegation(c, task, request, **_unused):
    """Explicit control facts only. Returns and adoption do not determine execution."""
    if request['kind'] not in ('plan', 'review'):
        return None
    control = _delegation_decisions(c, task['id']).get(request['id'])
    return {'state': 'ended' if control and control['action'] == 'end' else task['state'],
            'control': control}


def apply(c, root, action, actor, payload, *, prepared=None):
    """Apply one authorized coordination fact inside an existing transaction.

    Extensions may use this within transaction() so observations and their
    original-task request are committed together, or neither is committed.
    """
    root, actor = Path(root).resolve(), verified_actor(root, actor)
    ensure_schema(c)
    if not c.in_transaction:
        raise ValueError('apply requires the shared BEGIN IMMEDIATE transaction')
    if not isinstance(payload, dict):
        raise ValueError('Action payload must be an object')
    if action == 'complete':
        # This must precede both generic action-ID paths: pending observations
        # do not consume successful completion, including after reopen.
        from harness_scope import complete
        return complete(c,root,actor,payload,prepared)
    if action in ('business-bind', 'business-observe'):
        from task_ledger import business as harness_business
        return harness_business.apply(c, root, action, actor, payload)
    ident = _text(payload.get('id'), 'Stable action id')
    if action == 'run-review-delivered':
        from scientific_review.delivery import record
        return record(c,root,actor,payload)
    if action == 'run-delivery-evidence':
        from delivery_evidence import CLI_SCHEMA, operation
        run = _row(c, 'work_runs', ident)
        ref = evidence_ref(root, payload.get('record_ref'))
        record = json.loads(Path(ref['path']).read_bytes())
        if (record.get('schema') != CLI_SCHEMA or record.get('source') != 'trusted_public_cli'
                or record.get('run_id') != ident or record.get('request_id') != run['request_id']
                or operation(record.get('argv')) != record.get('operation')):
            raise ValueError('Native operation record must retain its exact run and public command')
        encoded = _json({'record_ref':ref})
        if not c.execute("SELECT 1 FROM work_run_events WHERE run_id=? AND kind='delivery-evidence' AND payload_json=?",
                         (ident,encoded)).fetchone():
            _run_event(c,run,'delivery-evidence',actor,None,{'record_ref':ref})
        return {'run_id':ident,'record_ref':ref,'business_outcome_unchanged':True}
    if action == 'run-capacity':
        return _record_capacity(c,root,actor,payload)
    if action == 'run-friction':
        run = _row(c, 'work_runs', payload.get('run_id'))
        ref = evidence_ref(root, payload.get('receipt'))
        observation = json.loads(Path(ref['path']).read_bytes())
        if (ident != 'runtime-friction:' + run['id'] or observation.get('run_id') != run['id']
                or observation.get('request_id') != run['request_id']
                or observation.get('config_sha256') != json.loads(run['config_ref_json'])['sha256']
                or observation.get('source') not in ('adapter_exception', 'native_rpc', 'native_cli_json')
                or not c.execute('SELECT 1 FROM work_run_events WHERE run_id=? AND receipt_json=?',
                                 (run['id'], _json(ref))).fetchone()):
            raise ValueError('Runtime friction requires the exact original native observation')
        failures = observation.get('result', {}).get('tool_failures', []) if isinstance(observation.get('result'), dict) else []
        tool_failure = isinstance(failures, list) and bool(failures) and all(
            isinstance(f, dict) and isinstance(f.get('native_item_id'), str) and
            (f.get('kind') == 'tool_error' or f.get('kind') == 'nonzero_exit' and
             isinstance(f.get('exit_code'), int) and not isinstance(f['exit_code'], bool) and f['exit_code'] != 0)
            for f in failures)
        if run['outcome'] not in ('failed', 'unknown') and not tool_failure:
            raise ValueError('Expected pauses and successful runs are not maintenance failures')
        req = _row(c, 'work_requests', run['request_id'])
        task = _row(c, 'work_tasks', req['task_id'])
        existing = c.execute('SELECT * FROM work_requests WHERE id=?', (ident,)).fetchone()
        if existing:
            if existing['task_id'] != task['id'] or existing['parent_request_id'] != req['id']:
                _fail('idempotency_conflict', 'Runtime finding identity belongs to another original request', ident)
            if tool_failure:
                from task_ledger import maintenance as harness_maintenance
                harness_maintenance.runtime_observation(c, root, task, actor, existing, ref, failures)
            return _decode(existing)
        fields = {'task_id': task['id'], 'kind': 'finding', 'sender_actor': actor_key(actor),
            'recipient_actor': None, 'recipient_role': 'maintainer', 'input_ref_json': _json(ref),
            'summary': _text(payload.get('summary'), 'Observed runtime failure, not an inferred root cause'),
            'plan_revision': task['plan_revision'], 'required_scope_json': '[]', 'independent': 0,
            'requires_review': 0, 'authors_json': '[]', 'parent_request_id': req['id'], 'supersedes_request_id': None}
        return _decode(_immutable(c, 'work_requests', ident, fields))
    if action == 'grant-role':
        target = _actor(root, payload.get('actor'), observed=True)
        role = payload.get('org_role')
        if role not in ORG_ROLES:
            raise ValueError('Organizational roles are distinct from data/calculator/coordinator capabilities')
        evidence = evidence_ref(root, payload.get('evidence_ref'))
        existing = c.execute('SELECT * FROM work_roles WHERE actor_key=? AND org_role=?', (actor_key(target), role)).fetchone()
        if existing is not None:
            if existing['evidence_ref_json'] != _json(evidence):
                _fail('role_binding_conflict', 'Existing responsibility grant is immutable')
            return _decode(existing)
        c.execute('INSERT INTO work_roles VALUES (?,?,?,?,?,?)',
                  (actor_key(target), role, _json(target), actor_key(actor), _json(evidence), _now()))
        return {'actor': target, 'org_role': role}
    if action == 'create':
        owner = _actor(root, payload.get('owner_actor'), observed=True)
        fields = {'original_ref_json': _json(evidence_ref(root, payload.get('original_ref'))),
                  'plan_ref_json': _json(prepared['plan'] if prepared else _plan_ref(root, payload.get('plan_ref'))),
                  'owner_actor': actor_key(owner), 'sponsor_actor': actor_key(actor),
                  'summary': _text(payload.get('summary'), 'Task purpose'),
                  'required_scope_json': _json(_list(payload.get('required_scope', []), 'Declared task scope')),
                  'revision': 1, 'plan_revision': 1, 'state': 'active'}
        row = c.execute('SELECT * FROM work_tasks WHERE id=?', (ident,)).fetchone()
        if row is not None:
            # Retries compare the immutable creation event, rather than a later plan.
            event = c.execute("SELECT payload_json FROM work_task_events WHERE task_id=? AND kind='create' ORDER BY id LIMIT 1", (ident,)).fetchone()
            prior = json.loads(event[0]) if event is not None else None
            if prior is None or any(prior.get(key) != value for key, value in fields.items()):
                _fail('idempotency_conflict', 'Task identity already belongs to a different original complete task', ident)
            return _decode(row)
        stamp = _now()
        c.execute('INSERT INTO work_tasks(id,' + ','.join(fields) + ',created_at,updated_at) VALUES (' + ','.join('?' for _ in range(len(fields) + 3)) + ')',
                  (ident, *fields.values(), stamp, stamp))
        task = _row(c, 'work_tasks', ident)
        if payload.get('delivery_contract') is not None:
            from harness_scope import set_contract
            set_contract(c,root,'task',ident,payload['delivery_contract'],payload['original_ref'],actor)
            task=_row(c,'work_tasks',ident)
        _event(c, task, 'create', actor, {**fields, 'baseline': _freeze_plan(c, root, task)})
        return _decode(task)
    if action in ('revise', 'reopen', 'pause', 'resume', 'cancel', 'transfer'):
        task = _row(c, 'work_tasks', payload.get('task_id', ident))
        for event in c.execute("""SELECT * FROM work_task_events WHERE task_id=? AND kind=?
                AND json_extract(payload_json,'$.id')=? ORDER BY id LIMIT 1""", (task['id'], action, ident)):
            saved = json.loads(event['payload_json'])
            if saved.get('id') != ident:
                continue
            if 'action_payload' not in saved:
                _fail('historical_action_receipt_missing', 'This historical action predates retry receipts; inspect its original event instead of applying it again', ident)
            def comparable(value):
                if action!='revise' or value.get('base_sha256') is None:
                    return value
                return {**value,'evidence_ref':value['evidence_ref']['sha256'],'plan_ref':value['plan_ref']['sha256'],
                        'affected_scope':sorted(value.get('affected_scope',[])),
                        'affected_request_ids':sorted(value.get('affected_request_ids',[]))}
            if native_key(event['actor_json']) != native_key(actor) or comparable(saved['action_payload']) != comparable(payload):
                _fail('idempotency_conflict', 'The original action id must retain its actor and complete payload, including expected revision', ident)
            return saved['result']
        human_recovery = None
        if action == 'revise' and payload.get('human_authorization_ref'):
            # A direct user instruction can authorize this maintainer's edit
            # without appointing it as lead or granting scientific ownership.
            human_recovery = evidence_ref(root, payload['human_authorization_ref'])
        elif action == 'transfer' and payload.get('human_authorization_ref'):
            human_recovery = evidence_ref(root, payload['human_authorization_ref'])
        explicit_baseline = action == 'revise' and payload.get('base_sha256') is not None
        previous_plan = json.loads(task['plan_ref_json'])
        if explicit_baseline:
            if native_key(actor) not in (native_key(task['owner_actor']), native_key(task['sponsor_actor'])) and not human_recovery:
                _fail('plan_authority_required', 'The current owner, original sponsor or explicitly delegated maintainer must adopt the baseline.', task['id'])
            if previous_plan['sha256'] != payload['base_sha256']:
                _fail('plan_baseline_conflict', 'Read the current frozen commitment and compare the proposed change before retrying.', task['id'])
        else:
            _revision(task, payload)
        evidence = prepared['reason'] if explicit_baseline and prepared else evidence_ref(root, payload.get('evidence_ref'))
        summary = _text(payload.get('summary'), 'Decision and reason')
        if action in ('revise', 'reopen'):
            if action == 'reopen' and task['state'] != 'completed':
                _fail('reopen_requires_completed', 'Use work-reopen for a completed original task; use work-resume for a paused task. Cancelled work retains its stop.', task['id'])
            if action == 'revise' and task['state'] in ('cancelled', 'completed'):
                _fail('closed_task', 'Continue a completed original task through work-reopen with the owner decision. Cancelled work retains its stop.', task['id'])
            old_scope = json.loads(task['required_scope_json'])
            new_scope = _list(payload.get('required_scope', old_scope), 'Declared task scope')
            affected = payload.get('affected_scope', [])
            targets = payload.get('affected_request_ids')
            plan = prepared['plan'] if prepared and 'plan' in prepared else _plan_ref(root, payload.get('plan_ref', previous_plan if action == 'reopen' else None))
            if action == 'revise' and plan['sha256'] == json.loads(task['plan_ref_json'])['sha256']:
                raise ValueError('A plan revision must bind genuinely changed original bytes')
            previous_state = task['state']
            task = _touch(c, task, plan_ref_json=_json(plan), plan_revision=task['plan_revision'] + 1,
                          required_scope_json=_json(new_scope), state='active' if action == 'reopen' else task['state'])
            if explicit_baseline and payload.get('delivery_contract') is not None:
                from harness_scope import set_contract
                set_contract(c, root, 'task', task['id'], payload['delivery_contract'], evidence, actor)
                task = _row(c, 'work_tasks', task['id'])
            _event(c, task, action, actor, {'id': ident, 'evidence_ref': evidence, 'summary': summary,
                                          'affected_scope': affected, 'plan_ref': plan,
                                          'previous_required_scope': old_scope, 'required_scope': new_scope,
                                          'previous_state': previous_state,
                                          'baseline': _freeze_plan(c, root, task, previous_plan, evidence),
                                          'affects_all_unsent': explicit_baseline and not (affected or targets),
                                          **({'human_authorization_ref': human_recovery} if human_recovery else {}),
                                          **({'affected_request_ids': targets} if targets is not None else {})})
        elif action == 'transfer':
            new_owner = _actor(root, payload.get('new_owner_actor'), observed=True)
            if native_key(new_owner) == native_key(task['owner_actor']):
                raise ValueError('Select a different actual task owner for this transfer')
            previous_owner = json.loads(task['owner_actor'])
            task = _touch(c, task, owner_actor=actor_key(new_owner))
            _event(c, task, action, actor, {'id': ident, 'evidence_ref': evidence,
                                          'summary': summary, 'previous_owner': previous_owner,
                                          'new_owner': new_owner, 'automatic_resume': False, 'human_authorization_ref': human_recovery})
        else:
            states = {'pause': 'paused', 'resume': 'active', 'cancel': 'cancelled'}
            if task['state'] in ('completed', 'cancelled'):
                _fail('closed_task', 'Closed task does not automatically resume', task['id'])
            if action == 'resume' and task['state'] != 'paused':
                _fail('not_paused', 'Only explicitly paused work may be resumed', task['id'])
            task = _touch(c, task, state=states[action])
            _event(c, task, action, actor, {'id': ident, 'evidence_ref': evidence, 'summary': summary})
        if action in ('pause', 'cancel'):
            for run in [run for request in c.execute('SELECT id FROM work_requests WHERE task_id=?', (task['id'],)).fetchall()
                        for run in pending_runs(c, request['id'])]:
                if not run['stop_requested']:
                    c.execute('UPDATE work_runs SET stop_requested=1,updated_at=? WHERE id=?', (_now(), run['id']))
                    _run_event(c, run, 'stop_requested', actor, None, {'reason': summary, 'task_change': action,
                        'meaning': 'Original foreground runtime/host must confirm actual stopping; unknown effects stay native.'})
        event = c.execute('SELECT * FROM work_task_events WHERE task_id=? ORDER BY id DESC LIMIT 1', (task['id'],)).fetchone()
        saved = json.loads(event['payload_json'])
        saved.update(action_payload=payload, result=_decode(task))
        c.execute('UPDATE work_task_events SET payload_json=? WHERE id=?', (_json(saved), event['id']))
        return _decode(task)
    if action == 'request':
        task = _row(c, 'work_tasks', payload.get('task_id'))
        _revision(task, payload, False)
        kind = payload.get('kind')
        if kind not in REQUEST_KINDS:
            raise ValueError('Choose plan, review, finding or feedback.')
        ref = evidence_ref(root, payload.get('input_ref'))
        recipient = _actor(root, payload['recipient'], observed=True) if payload.get('recipient') else None
        recipient_role = payload.get('recipient_role')
        if recipient_role not in ORG_ROLES:
            raise ValueError('Identify the intended responsibility.')
        scope = _scope(task, payload.get('required_scope', []), 'Declared request scope', False)
        independent = bool(payload.get('independent', False))
        parent = payload.get('parent_request_id')
        parent_request = _row(c, 'work_requests', parent) if parent else None
        if parent_request is not None and parent_request['task_id'] != task['id']:
            raise ValueError('The selected original request belongs to another task.')
        if independent and (kind != 'review' or recipient_role != 'executor' or parent_request is None):
            raise ValueError('Identify the original work to review independently.')
        authors = request_authors(c, parent_request) if independent else []
        supersedes = payload.get('supersedes_request_id')
        if supersedes:
            previous = _row(c, 'work_requests', supersedes)
            if kind != 'feedback' or previous['kind'] != 'feedback' or previous['task_id'] != task['id']:
                raise ValueError('Only a revised feedback observation may supersede the same task\'s original feedback request')
        fields = {'task_id': task['id'], 'kind': kind, 'sender_actor': actor_key(actor),
                  'recipient_actor': actor_key(recipient) if recipient else None, 'recipient_role': recipient_role,
                  'input_ref_json': _json(ref), 'summary': _text(payload.get('summary'), 'Requested work and purpose'),
                  'plan_revision': task['plan_revision'], 'required_scope_json': _json(scope),
                  'independent': int(independent), 'requires_review': int(bool(payload.get('requires_review', False))),
                  'authors_json': _json(authors), 'parent_request_id': parent,
                  'supersedes_request_id': supersedes}
        if independent and recipient:
            _independent(fields | {'id': ident}, task, recipient)
        existing = c.execute('SELECT * FROM work_requests WHERE id=?', (ident,)).fetchone()
        is_author = finite.programmatic_request(c, fields)
        if independent and not existing:
            from scientific_review.rounds import reserve
            _admit_scope(c, task)
            reserve(c,root,task,actor,parent_request,ident,prepared)
        if existing and fields['recipient_actor'] is None and existing['recipient_actor'] is not None:
            claimed = any(json.loads(e[0]).get('request_id') == ident for e in c.execute(
                "SELECT payload_json FROM work_task_events WHERE task_id=? AND kind IN ('maintenance-claim','native-bind')", (task['id'],)))
            if claimed:
                fields['recipient_actor'] = existing['recipient_actor']
        result = _decode(_immutable(c, 'work_requests', ident, fields))
        if is_author and not existing:
            work_config = {}
            if previous := payload.get('continues_request_id'):
                from harness_scope import _owner, effective_contract, request_progress
                _owner(c,task,actor)
                original = _row(c,'work_requests',previous)
                if (original['task_id']!=task['id'] or original['independent'] or original['kind']!='plan'
                        or set(json.loads(original['required_scope_json']))!=set(scope)
                        or finite.successor(finite.request_configs(c,task['id']),previous)):
                    raise ValueError('Continue the same exact delivery unit only once, preserving its task and original scope')
                if pending_runs(c,previous) or _delegation_decisions(c,task['id']).get(previous,{}).get('action')=='end':
                    from harness_package import ended_devin_writer
                    if not ended_devin_writer(c,root,previous):
                        _fail('package_recovery_required','Resolve the original execution, effects and stop first.',previous)
                original=_decode(original)
                if request_progress(c,original)['delivered']:
                    _fail('original_delivered','The original unit is delivered. Reopen its exact correction or assign a new independent use.',previous)
                original_use=effective_contract(c,original)['body']
                changes=payload.get('delivery_contract') or {}
                if any(key in changes and changes[key]!=original_use.get(key) for key in ('purpose','endpoint')):
                    _fail('continuation_changes_use','A different purpose or endpoint needs a new original request and Session.',previous)
                work_config['continues_request_id']=previous
            if payload.get('package_input_refs'):
                work_config['package_input_refs'] = _refs(root, payload['package_input_refs'], 'Original package inputs', True)
            _event(c, task, 'finite-request', actor, {'request_id': ident, 'limits': work_config})
        if payload.get('delivery_contract') is not None:
            from harness_scope import set_contract
            set_contract(c,root,'request',ident,payload['delivery_contract'],ref,actor)
        return result
    if action == 'return':
        req = _row(c, 'work_requests', payload.get('request_id'))
        task = _row(c, 'work_tasks', req['task_id'])
        run_id, receipt, assurance = payload.get('run_id'), None, 'current_native_environment'
        if run_id:
            run = _row(c, 'work_runs', run_id)
            if run['request_id'] != req['id'] or not run['native_identity_json']:
                _fail('return_run_conflict', 'Return must bind the original request and observed native run', ident)
            receipt, doc, author = _native_receipt(root, payload.get('native_receipt'), run)
            if native_key(author) != native_key(run['native_identity_json']):
                _fail('return_author_conflict', 'Native returned author differs from the original bound run', ident)
            assurance = 'dispatcher_observed_native_receipt'
        else:
            author = actor
        _independent(req, task, author)
        validate_return_payload(root,_decode(req),payload)
        coverage = _list(payload.get('coverage', []), 'Actual returned coverage')
        remaining = _list(payload.get('remaining_scope', []), 'Explicit remaining request scope')
        fields = {'request_id': req['id'], 'run_id': run_id, 'plan_revision': req['plan_revision'],
                  'author_actor': actor_key(author), 'identity_assurance': assurance,
                  'result_ref_json': _json(evidence_ref(root, payload.get('result_ref'))),
                  'artifact_refs_json': _json(_artifact_refs(root, payload.get('artifact_refs', []))),
                  'coverage_json': _json(coverage), 'remaining_scope_json': _json(remaining),
                  'summary': _text(payload.get('summary'), 'Actual contribution, changes and limits'),
                  'registered_by': actor_key(actor), 'native_receipt_json': _json(receipt) if receipt else None}
        # A late return is saved accurately even after pause/correction. Adoption
        # is a separate current-plan action and cannot inherit old completion.
        source_hash = json.loads(fields['result_ref_json'])['sha256']
        same_version = c.execute('''SELECT * FROM work_returns WHERE request_id=? AND run_id IS ?
                                    AND json_extract(result_ref_json, '$.sha256')=? AND author_actor=?''',
                                 (req['id'], run_id, source_hash, fields['author_actor'])).fetchone()
        original = c.execute('SELECT * FROM work_returns WHERE id=?', (ident,)).fetchone()
        if original and (any(original[key] != fields[key] for key in ('request_id', 'run_id', 'author_actor'))
                         or json.loads(original['result_ref_json'])['sha256'] != source_hash):
            _fail('return_version_conflict', 'Return identity already names a different author, run or saved result; retain both originals.', ident)
        retained = original or same_version
        if retained:
            # Observation receipts and registration metadata describe a reading
            # of the result, not a new result. This runs in the same transaction
            # as insertion, so observer-first and finisher-first are equivalent.
            if any(retained[key] != value for key, value in fields.items()):
                _event(c, task, 'return-observation', actor, {'return_id': retained['id'],
                       'native_receipt': receipt, 'result_ref': json.loads(fields['result_ref_json']),
                       'summary': fields['summary'],
                       **({'artifact_refs':json.loads(fields['artifact_refs_json'])} if 'artifact_refs' in payload else {}),
                       **({'coverage':coverage} if 'coverage' in payload else {}),
                       **({'remaining_scope':remaining} if 'remaining_scope' in payload else {})})
            result = _decode(retained)
        else:
            result = _decode(_immutable(c, 'work_returns', ident, fields))
        if req['independent'] and prepared:
            from scientific_review.rounds import record_opinion
            record_opinion(c,root,task,req,result,prepared)
        if req['kind'] == 'finding' and req['recipient_role'] == 'executor':
            from task_ledger import maintenance as harness_maintenance
            parent = harness_maintenance.parent_finding(c, req, current_owner=False)
            if parent is not None:
                result.update(maintenance_finding_id=parent['id'],
                              maintenance_owner=json.loads(parent['recipient_actor']),
                              maintenance_status='pending_owner_review')
        return result
    if action in ('consume', 'dispose'):
        ret = _row(c, 'work_returns', payload.get('return_id'))
        req = _row(c, 'work_requests', ret['request_id'])
        task = _row(c, 'work_tasks', req['task_id'])
        evidence = prepared['note'] if prepared else evidence_ref(root, payload.get('evidence_ref'))
        fields = {'return_id':ret['id'], 'actor_json':_json(actor),
                  'evidence_ref_json':_json(evidence), 'summary':_text(payload.get('summary'), 'Owner judgment')}
        if action == 'consume':
            return _decode(_immutable(c, 'work_consumptions', ident, fields))
        from harness_scope import _owner
        _owner(c,task,actor)
        disposition = payload.get('disposition')
        if disposition not in ('adopt','revise','hold','reject'):
            raise ValueError('Choose the owner action: adopt, revise, hold or reject.')
        acceptance=payload.get('acceptance','stage')
        if acceptance not in ('stage','final') or acceptance=='final' and disposition!='adopt':
            raise ValueError('Final acceptance is only available for adopt; otherwise use stage.')
        scope=_list(payload.get('scope',[]),'Adopted scope IDs')
        applied=_list(payload.get('apply_review_return_ids',[]),'Original independent reviews whose applicability the lead accepts')
        applications=[]
        if applied:
            if disposition!='adopt':raise ValueError('Scientific applicability requires the task lead\'s explicit adopt judgment')
            if prepared is None:
                raise ValueError('Prepare scoped review evidence through the original disposition entry point.')
            applications=prepared['applications']
            ident='review-application:'+hashlib.sha256(_json([ident,applications]).encode()).hexdigest()
        elif payload.get('handling_refs'):raise ValueError('Use --apply-review to identify the exact original opinion for this handling')
        check_report=check_ref=None
        if acceptance=='final':
            import harness_scope
            if prepared is None:
                raise ValueError('Prepare final evidence through the original disposition entry point.')
            check_report,check_ref=prepared['report'],prepared['check_ref']
            ident='final-adoption:'+hashlib.sha256(_json([ident,prepared['choice']]).encode()).hexdigest()
        if prepared:
            import harness_scope
            harness_scope._owner(c,task,actor)
            if not c.execute('SELECT 1 FROM work_dispositions WHERE id=?',(ident,)).fetchone():
                harness_scope.verify_disposition(c,task,_decode(req),ret['id'],scope,prepared)
        supersedes = _list(payload.get('supersedes', []), 'Superseded decisions')
        for previous in supersedes:
            if _row(c, 'work_dispositions', previous)['return_id'] != ret['id']:
                raise ValueError('A superseding decision must refer to the same original return.')
        reviews = list(dict.fromkeys([*_list(payload.get('review_return_ids', []), 'Cited independent reviews'),*applied]))
        for review_id in reviews:
            review = _row(c, 'work_returns', review_id)
            original = _row(c, 'work_requests', review['request_id'])
            if (original['task_id'] != task['id'] or not original['independent']
                    or review['author_actor'] == ret['author_actor']):
                raise ValueError('Cited independent review must be a non-author Session in this task.')
        fields.update(disposition=disposition, scope_json=_json(scope),
            remaining_scope_json=_json(payload.get('remaining_scope', [])), responsible_actor=task['owner_actor'],
            review_return_ids_json=_json(reviews), conditions_json=_json(payload.get('conditions', [])),
            supersedes_json=_json(supersedes), plan_revision=task['plan_revision'], compatibility_ref_json=None)
        result = _immutable(c, 'work_dispositions', ident, fields)
        _immutable(c, 'work_consumptions', 'disposition:' + ident,
            {'return_id':ret['id'], 'actor_json':_json(actor), 'evidence_ref_json':_json(evidence), 'summary':fields['summary']})
        import harness_scope
        if not harness_scope._evidence_events(c,task['id'],'delivery-acceptance-recorded','disposition_id',[ident]):
            endpoint=harness_scope.endpoint(harness_scope.effective_contract(c,_decode(req)))
            checked=(check_report or {}).get('scope_endpoints',{}).get(ret['id'],{})
            endpoint_by_scope={sid:checked.get(sid,{}).get('checked_endpoint',endpoint) for sid in scope}
            metadata={'disposition_id':ident,'acceptance':acceptance,'check_ref':check_ref,
                **({'adopted_scope_ids':scope,'original_author_coverage':_decode(ret)['coverage'],
                    'candidate_refs':prepared['choice'][1],'candidate_subjects':prepared['candidate_subjects']} if acceptance=='final' else {}),
                **({'review_applications':applications} if applications else {}),
                'request_endpoint':endpoint,
                'endpoint_by_scope':endpoint_by_scope,
                'submitted_scope_ids':[sid for sid in scope if check_report and
                                      endpoint_by_scope[sid] in ('submitted','published')]}
            _event(c,task,'delivery-acceptance-recorded',actor,metadata)
        decoded=_decode(result)
        if bookmark:=payload.get('wave_bookmark'):
            from harness_wave import record_wave_review
            record_wave_review(c,task,actor,bookmark,decoded)
        return decoded
    if action == 'run-intent':
        req = _row(c, 'work_requests', payload.get('request_id'))
        task = _row(c, 'work_tasks', req['task_id'])
        _revision(task, payload, False)
        expected = payload.get('plan_revision')
        if expected is not None and expected != req['plan_revision']:
            _fail('run_plan_conflict', 'Run intent must retain its original request plan revision', req['id'])
        _admit_request(c, task, req, side_effect=True)
        config = evidence_ref(root, payload.get('config_ref'))
        inputs = _refs(root, payload.get('input_refs'), 'Exact run input references', True)
        host = payload.get('target_host')
        from harness_hosts import HOSTS
        if host not in HOSTS:
            raise ValueError('Unsupported native target host')
        fields = {'request_id': req['id'], 'dispatcher_actor': actor_key(actor),
                  'plan_revision': req['plan_revision'], 'task_revision': task['revision'],
                  'config_ref_json': _json(config), 'target_host': host,
                  'operation': _text(payload.get('operation'), 'Native operation')}
        auto_from = payload.get('dsh_auto_continue_from')
        if auto_from:
            from dsh_auto_resume import guard
            if (fields['operation']!='resume' or host!='deepseek_harness' or auto_from!=payload.get('previous_run_id')
                    or _row(c,'work_runs',auto_from)['request_id']!=req['id']
                    or _row(c,'work_runs',auto_from)['config_ref_json']!=fields['config_ref_json']):
                _fail('auto_resume_no_authority','Automatic recovery retains the exact DSH attempt.',ident)
            guard(c,root,auto_from,ident,entering=True)
        row = c.execute('SELECT * FROM work_runs WHERE id=?', (ident,)).fetchone()
        if row is not None:
            expected = inputs
            if any(row[key] != value for key, value in fields.items() if key != 'dispatcher_actor') or json.loads(row['input_refs_json']) != expected:
                _fail('idempotency_conflict', 'An existing native intent is immutable; recover it rather than launch again', ident)
            return _run_view(c, row) | {'may_launch': False}
        previous_run_id = payload.get('previous_run_id')
        ordinary=payload.get('ordinary_continuation')
        if ordinary:
            from harness_scope import continuation
            if fields['operation']!='resume':
                raise ValueError('An owner continuation must retain the original native attempt')
            if isinstance(continuation(c,root,_decode(req),ordinary.get('basis')),dict) and not ordinary.get('basis'):
                _fail('original_delivered','The original unit reached its endpoint before continuation.',req['id'])
        if fields['operation'] == 'resume' and previous_run_id:
            previous_run = _row(c, 'work_runs', previous_run_id)
            if previous_run['request_id'] == req['id']:
                latest = c.execute('SELECT id FROM work_runs WHERE request_id=? ORDER BY created_at DESC,id DESC LIMIT 1',
                                   (req['id'],)).fetchone()
                if latest['id'] != previous_run_id:
                    _fail('run_recovery_required', 'An original successor already owns this Session turn', latest['id'])
        from task_ledger import maintenance as harness_maintenance
        parent = harness_maintenance.parent_finding(c, req)
        if parent is not None:
            for sibling in c.execute('SELECT id FROM work_requests WHERE parent_request_id=? AND id!=?',
                                     (parent['id'], req['id'])).fetchall():
                if pending_runs(c, sibling['id']):
                    _fail('repair_writer_active', 'An original repair attempt is still active or unknown; reconcile it before another writer starts', sibling['id'])
        for previous in pending_runs(c, req['id']):
            if (previous['id'] == payload.get('previous_run_id') and fields['operation'] == 'resume'
                    and previous['outcome'] == 'unknown' and host == previous['target_host']
                    and supports(host, 'interrupted_resume')):
                session_origin(c, previous)  # Native ownership is acquired by the selected host, never replaced.
            else:
                _fail('run_recovery_required', 'Original intent/run is in-flight or unknown; reconcile ' + previous['id'] + ' before any new launch', ident)
        replacement=None
        if req['id'] in finite.request_configs(c, task['id']):
            from harness_package import admit_author_run
            replacement=admit_author_run(c,req,fields['operation'],payload.get('previous_run_id'))
        if payload.get('writer_replacement') and payload['writer_replacement']!=replacement:
            _fail('writer_handoff_changed','Recheck the original writer and exact retained effects before starting a successor.',ident)
        fields['input_refs_json'] = _json(inputs)
        stamp = _now()
        c.execute('INSERT INTO work_runs(id,' + ','.join(fields) + ',outcome,native_identity_json,stop_requested,created_at,updated_at) VALUES (' + ','.join('?' for _ in range(len(fields) + 6)) + ')',
                  (ident, *fields.values(), 'intent', None, 0, stamp, stamp))
        row = _row(c, 'work_runs', ident)
        _run_event(c, row, 'intent', actor, None, {'request_id': req['id'],
            **({'dsh_auto_continue_from':auto_from} if auto_from else {}),
            **({'ordinary_continuation':ordinary} if ordinary else {}),
            **({'writer_replacement':replacement} if replacement else {}),
            **({'previous_run_id': previous_run_id} if previous_run_id else {}),})
        if host == 'deepseek_harness' and (watch := payload.get('dsh_recovery_watch')):
            from dsh_auto_resume import WATCH
            from dsh_capacity import recovery_managed
            if (watch.get('manager', {}).get('pid') != os.getpid()
                    or not recovery_managed(root, watch.get('provider'))):
                _fail('auto_resume_no_authority', 'Observe the actual configured native execution manager', ident)
            _run_event(c, row, WATCH, actor, None, watch)
        return _decode(row) | {'may_launch': True,}
    if action in ('run-bind', 'run-outcome', 'run-stop'):
        run = _row(c, 'work_runs', ident)
        req = _row(c, 'work_requests', run['request_id'])
        task = _row(c, 'work_tasks', req['task_id'])
        if action == 'run-stop':
            # Requesting stop remains permitted after pause/correction. Only a
            # native receipt can subsequently establish confirmed stopping.
            if not run['stop_requested']:
                c.execute('UPDATE work_runs SET stop_requested=1,updated_at=? WHERE id=?', (_now(), ident))
                _run_event(c, run, 'stop_requested', actor, None, {'reason': _text(payload.get('summary'), 'Stop reason')})
            return _decode(_row(c, 'work_runs', ident))
        diagnostic = None
        expected_control_stop = False
        diagnostic_outcome = payload.get('outcome')
        if action == 'run-outcome' and diagnostic_outcome in ('unknown', 'failed') and payload.get('receipt'):
            diagnostic = evidence_ref(root, payload['receipt'])
            observation = json.loads(Path(diagnostic['path']).read_bytes())
            if observation.get('source') == 'adapter_exception':
                if (observation.get('schema') != 'data-workspace.agent-runtime-observation.v1'
                        or observation.get('run_id') != run['id'] or observation.get('request_id') != run['request_id']
                        or observation.get('config_sha256') != json.loads(run['config_ref_json'])['sha256']):
                    raise ValueError('Adapter failure must retain the exact original run, request and configuration')
                if diagnostic_outcome == 'failed':
                    from harness_hosts import confirmed_not_started
                    ready_thread = json.loads(run['native_identity_json']) if run['native_identity_json'] else None
                    if not confirmed_not_started(
                            observation, host=run['target_host'], run_id=run['id'], request_id=run['request_id'],
                            config_sha256=json.loads(run['config_ref_json'])['sha256'], native_identity=ready_thread):
                        raise ValueError('A rejected startup must retain its confirmed pre-opening diagnostic.')
                if observation.get('error_type') == 'StopRequested' and observation.get('control_stop_run_id'):
                    stopped = c.execute('SELECT * FROM work_runs WHERE id=?',
                                        (observation['control_stop_run_id'],)).fetchone()
                    request = _row(c, 'work_requests', run['request_id'])
                    expected_control_stop = bool(stopped and stopped['stop_requested']
                        and stopped['request_id'] in (request['id'], request['parent_request_id'])
                        and _row(c, 'work_requests', stopped['request_id'])['task_id'] == request['task_id'])
            else:
                diagnostic = None
        if action == 'run-outcome' and (diagnostic or diagnostic_outcome == 'unknown' and not payload.get('receipt')):
            if run['outcome'] in ('succeeded', 'failed', 'stopped'):
                _fail('terminal_run', 'Known original terminal facts cannot be overwritten by an adapter diagnostic', ident)
            c.execute('UPDATE work_runs SET outcome=?,updated_at=? WHERE id=?', (diagnostic_outcome, _now(), ident))
            _run_event(c, run, diagnostic_outcome, actor, diagnostic,
                       {'summary': _text(payload.get('summary'), 'Original diagnostic and recovery point')})
            if diagnostic and not expected_control_stop:
                apply(c, root, 'run-friction', actor, {'id': 'runtime-friction:' + ident, 'run_id': ident,
                    'receipt': diagnostic, 'summary': 'Native adapter failed; see original run ' + ident + ' and its recorded host diagnostic.'})
            return _decode(_row(c, 'work_runs', ident))
        receipt, doc, native = _native_receipt(root, payload.get('receipt'), run, payload.get('native_identity'))
        previous = json.loads(run['native_identity_json']) if run['native_identity_json'] else None
        if previous is None and run['operation'] == 'resume':
            intent = c.execute("SELECT payload_json FROM work_run_events WHERE run_id=? AND kind='intent' ORDER BY id LIMIT 1", (run['id'],)).fetchone()
            prior_id = json.loads(intent['payload_json']).get('previous_run_id') if intent else None
            if prior_id:
                prior = _row(c, 'work_runs', prior_id)
                if prior['outcome'] == 'unknown' and supports(prior['target_host'], 'interrupted_resume'):
                    prior = session_origin(c, prior)
                if prior['native_identity_json'] and native_key(prior['native_identity_json']) != native_key(native):
                    _fail('native_identity_conflict', 'Continuation must retain its original author or reviewer Session', ident)
        if previous and native_key(previous) != native_key(native):
            _fail('native_identity_conflict', 'Original native session identity is immutable; a new run cannot replace it', ident)
        if previous and previous.get('turn_id') and native.get('turn_id') != previous['turn_id']:
            observed = doc.get('observation') or {}
            host = run['target_host']
            native_advance = (host == 'codex' and observed.get('source') == 'codex_turn_start'
                or host == 'deepseek_harness' and observed.get('source') == 'dsh_turn_start'
                and str(native.get('turn_id', '')).isdigit() and str(previous['turn_id']).isdigit()
                and int(native['turn_id']) > int(previous['turn_id']))
            continuation = (action == 'run-bind' and native_advance
                and run['outcome'] in ('pending', 'running', 'unknown') and not run['stop_requested']
                and observed.get('previous_turn') == {'turn_id': previous['turn_id'], 'terminal': 'completed'})
            if not continuation:
                _fail('native_turn_conflict', 'A later turn requires the same live native invocation and its completed predecessor', ident)
        if req['recipient_actor'] and native_key(req['recipient_actor']) != native_key(native):
            _fail('native_recipient_conflict', 'Native run differs from the request\'s exact assigned recipient', ident)
        _independent(req, task, native)
        outcome = run['outcome']
        if action == 'run-bind':
            if run['outcome'] in ('succeeded', 'failed', 'stopped'):
                if _json(previous) != _json(native):
                    _fail('terminal_run', 'Terminal binding cannot be changed', ident)
            else:
                outcome = 'running'
        else:
            outcome = payload.get('outcome')
            if outcome not in RUN_OUTCOMES[1:]:
                raise ValueError('Outcome must be an explicit observed running/terminal/unknown value')
            if outcome == 'stopped' and doc.get('stop_confirmed') is not True:
                _fail('stop_unconfirmed', 'Stop request/exit alone does not establish native confirmed stop', ident)
            status_map = {'succeeded': ('completed', 'succeeded'), 'failed': ('failed', 'error'),
                          'stopped': ('interrupted', 'stopped', 'cancelled'), 'running': ('running', 'in_progress', 'inProgress')}
            if outcome in status_map and doc.get('native_status') not in status_map[outcome]:
                _fail('native_status_conflict', 'Outcome differs from the actual native status observation', ident)
            if run['outcome'] in ('succeeded', 'failed', 'stopped') and run['outcome'] != outcome:
                _fail('terminal_run', 'Original terminal observation cannot be rewritten', ident)
        duplicate = c.execute('SELECT 1 FROM work_run_events WHERE run_id=? AND kind=? AND receipt_json=?',
                              (ident, action, _json(receipt))).fetchone()
        if not duplicate:
            c.execute('UPDATE work_runs SET native_identity_json=?,outcome=?,updated_at=? WHERE id=?',
                      (_json(native), outcome, _now(), ident))
            observation = {'outcome': outcome}
            result=doc.get('result')
            execution=result.get('execution_observation') if isinstance(result,dict) else None
            if isinstance(execution,dict) and execution.get('state') in ('active','exited','unknown'):
                observation['execution_observation']={k:execution[k] for k in
                    ('state','reason','source','native_result_state','data_effect_state') if k in execution}
            _run_event(c, run, action, actor, receipt, observation)
        return _decode(_row(c, 'work_runs', ident))
    raise ValueError('Unsupported complete-task action: ' + action)


def mutate(root, action, actor, payload):
    prepared = None
    if action in ('create', 'revise') and payload.get('plan_ref'):
        prepared = {'plan': _plan_ref(root, payload['plan_ref'])}
        if action == 'revise' and payload.get('base_sha256') is not None:
            prepared['reason'] = _plan_ref(root, payload['evidence_ref'])
    if action == 'request' and payload.get('independent'):
        from scientific_review.rounds import prepare
        prepared = prepare(root, payload)
    if action == 'return' and payload.get('request_id'):
        from scientific_review.rounds import prepare_opinion
        prepared = prepare_opinion(root, payload)
    if action == 'complete' or action == 'dispose' and (payload.get('acceptance') == 'final' or payload.get('apply_review_return_ids')):
        from harness_scope import prepare_decision
        prepared = prepare_decision(root,action,verified_actor(root,actor),payload)
    try:
        with transaction(root) as c:
            result = apply(c, root, action, actor, payload, prepared=prepared)
            if action in ('create', 'revise', 'reopen', 'request', 'return', 'run-outcome') and not (
                    action == 'revise' and 'request_id' in payload):
                # Original semantic metadata is indexed when it is actually used;
                # missing/invalid optional metadata never discards a real delivery.
                try:
                    knowledge = importlib.import_module('harness_knowledge')
                except ModuleNotFoundError as exc:
                    if exc.name != 'harness_knowledge':
                        raise
                else:
                    task_id = (result['id'] if action in ('create', 'revise', 'reopen') else result['task_id']
                               if action == 'request' else _row(c, 'work_requests', result['request_id'])['task_id'])
                    originals = ([result['original_ref'], result['plan_ref']] if action == 'create'
                                 else [result['plan_ref']] if action in ('revise', 'reopen')
                                 else [result['input_ref']] if action == 'request'
                                 else ([payload['receipt']] if payload.get('receipt') else []) if action == 'run-outcome'
                                 else [result['result_ref'], *result['artifact_refs']])
                    warnings = []
                    for original in originals:
                        c.execute('SAVEPOINT optional_knowledge_index')
                        try:
                            warnings.extend(knowledge.index_original(c, root, task_id, original, actor))
                        except (ValueError, TypeError, KeyError, OSError, sqlite3.Error) as exc:
                            c.execute('ROLLBACK TO optional_knowledge_index')
                            warnings.append({'source_ref': original, 'reason': str(exc)})
                        finally:
                            c.execute('RELEASE optional_knowledge_index')
                    if warnings:
                        result['knowledge_warnings'] = warnings
            return result
    except WorkConflict as error:
        if action == 'request' and error.code == 'review_round_budget':
            from scientific_review.rounds import record_held
            record_held(root, actor, payload, error.review_units.values())
            error.review_request = payload['id']
        raise


def admission(root, task_id, actor, *, request_id=None, expected_plan_revision=None, side_effect=False):
    """Gate used by real adapter launch/resume boundaries, not a data permit."""
    actor = verified_actor(root, actor)
    c = _connect(root, True)
    try:
        ensure_schema(c)
        task = _row(c, 'work_tasks', task_id)
        if request_id:
            req = _row(c, 'work_requests', request_id)
            if (req['task_id'] != task_id or
                    (expected_plan_revision is not None and req['plan_revision'] != expected_plan_revision)):
                _fail('stale_request', 'Request no longer binds this current task plan', request_id)
            admission_scope = _admit_request(c, task, req, side_effect=side_effect)
        else:
            _active(task)
            admission_scope = 'scientific_task'
        return {'admission_scope': admission_scope, 'task_id': task_id, 'task_revision': task['revision'],
                'plan_revision': req['plan_revision'] if request_id else task['plan_revision'],
                'state': task['state'], 'side_effect_gate': bool(side_effect),
                'native_authority': False, 'scope_note': 'Native owner, authorization, preimage and professional checks remain with the actual tool entry.'}
    finally:
        c.close()


@contextlib.contextmanager
def dispatch_admission(root, task_id, actor, *, request_id, run_id, expected_plan_revision=None):
    """Order a short actual native start/resume boundary with corrections.

    The caller holds this only around the native effect initiation, never a
    long-running wait, and records native observations after this transaction.
    A correction committed first rejects the effect; one committed afterwards
    observes an already initiated run and can request stopping the same run.
    """
    actor = verified_actor(root, actor)
    with transaction(root) as c:
        task = _row(c, 'work_tasks', task_id)
        req = _row(c, 'work_requests', request_id)
        run = _row(c, 'work_runs', run_id)
        if (req['task_id'] != task_id or run['request_id'] != request_id or
                (expected_plan_revision is not None and
                 (req['plan_revision'] != expected_plan_revision or run['plan_revision'] != expected_plan_revision))):
            _fail('dispatch_binding_conflict', 'Native effect must retain its exact original task, request, intent and plan', run_id)
        admission_scope = _admit_request(c, task, req, side_effect=True)
        input_issues = check_inputs(root, json.loads(run['input_refs_json']))
        if run['stop_requested'] or run['outcome'] not in ('intent', 'running'):
            _fail('dispatch_run_not_admitted', 'Original run is stopped, terminal or uncertain; reconcile its actual effects instead of restarting', run_id)
        intent = c.execute('SELECT payload_json FROM work_run_events WHERE run_id=? AND kind=\'intent\' ORDER BY id LIMIT 1',(run_id,)).fetchone()
        control=json.loads(intent[0]) if intent else {}
        if replacement:=control.get('writer_replacement'):
            from harness_package import missing_devin_writer
            if missing_devin_writer(c,root,req,replacement['request_id'])!=replacement:
                _fail('writer_handoff_changed','The ended writer, Session or retained effects changed before native start.',run_id)
        if ordinary:=control.get('ordinary_continuation'):
            from harness_scope import continuation
            if continuation(c,root,_decode(req),ordinary.get('basis')) and not ordinary.get('basis'):
                _fail('original_delivered','The original unit reached its endpoint before native continuation.',req['id'])
        if previous := control.get('dsh_auto_continue_from'):
            from dsh_auto_resume import guard
            guard(c,root,previous,run_id)
        yield {'admission_scope': admission_scope, 'task_id': task_id, 'request_id': request_id, 'run_id': run_id,
               'task_revision': task['revision'], 'plan_revision': run['plan_revision'],
               'native_authority': False, 'ordered_with_task_updates': True, 'input_issues': input_issues}


def _unresolved_requests(c, task):
    """Unreturned requests and open findings; no coverage/adoption arithmetic."""
    ended = _delegation_decisions(c, task['id'])
    successors = finite.request_configs(c, task['id'])
    superseded = {row[0] for row in c.execute('SELECT supersedes_request_id FROM work_requests WHERE task_id=? AND supersedes_request_id IS NOT NULL', (task['id'],))}
    from scientific_review.queue import EVENT, WITHHELD
    superseded.update(row[0] for row in c.execute("SELECT json_extract(payload_json,'$.superseded_review_id') "
        'FROM work_task_events WHERE task_id=? AND kind=?',(task['id'],EVENT)))
    held={row[0] for row in c.execute("SELECT json_extract(payload_json,'$.review_id') FROM work_task_events "
        'WHERE task_id=? AND kind=?',(task['id'],WITHHELD))}
    held.difference_update(row[0] for row in c.execute("SELECT json_extract(payload_json,'$.review_id') FROM work_task_events "
        'WHERE task_id=? AND kind=?',(task['id'],EVENT)))
    result = []
    for req in c.execute('SELECT * FROM work_requests WHERE task_id=? ORDER BY created_at,id', (task['id'],)):
        if req['id'] in superseded or req['id'] in held or ended.get(req['id'], {}).get('action') == 'end' or finite.successor(successors, req['id']):
            continue
        if req['kind'] in ('finding', 'feedback'):
            import harness_feedback
            handled = (harness_feedback.maintenance_is_disposed(c, req['id']) if req['kind'] == 'finding'
                       else harness_feedback.request_is_disposed(c, req['id']))
            if not handled:
                result.append(req['id'])
        elif not c.execute('SELECT 1 FROM work_returns WHERE request_id=?', (req['id'],)).fetchone():
            result.append(req['id'])
    return result


def _snapshot(c, task):
    """Original records only. A read does not decide what work is complete."""
    task_view = _decode(task)
    task_view['owner_actor_key'] = task['owner_actor']
    task_view['cycle'] = 1 + c.execute("SELECT count(*) FROM work_task_events WHERE task_id=? AND kind='reopen'", (task['id'],)).fetchone()[0]
    requests = [_decode(row) for row in c.execute('SELECT * FROM work_requests WHERE task_id=? ORDER BY created_at,id', (task['id'],))]
    returns = []
    for row in c.execute('SELECT r.* FROM work_returns r JOIN work_requests q ON q.id=r.request_id WHERE q.task_id=? ORDER BY r.created_at,r.id', (task['id'],)):
        value = _decode(row)
        value['consumed'] = bool(c.execute('SELECT 1 FROM work_consumptions WHERE return_id=?', (row['id'],)).fetchone())
        value['decisions'] = [_decode(d) for d in c.execute('SELECT * FROM work_dispositions WHERE return_id=? ORDER BY created_at,id', (row['id'],))]
        returns.append(value)
    runs = [_run_view(c, row) for row in c.execute('SELECT r.* FROM work_runs r JOIN work_requests q ON q.id=r.request_id WHERE q.task_id=? ORDER BY r.created_at,r.id', (task['id'],))]
    from task_ledger import business as harness_business
    root = workspace_root(c)
    return {'task': task_view, 'requests': requests, 'returns': returns, 'runs': runs,
            'dispositions': [_decode(row) for row in _effective_dispositions(c, task['id'])],
            'business': harness_business.business_projection(c, root, task),
            'original_links': harness_business.project(c, root, task['id']),
            'pending_request_ids': _unresolved_requests(c, task),
            'events': [_decode(row) for row in c.execute('SELECT * FROM work_task_events WHERE task_id=? ORDER BY id', (task['id'],))]}


def read_task(root, task_id):
    c = _connect(root, True)
    try:
        c.execute('BEGIN')
        ensure_schema(c)
        return _snapshot(c, _row(c, 'work_tasks', task_id))
    finally:
        c.close()


def _read_one(root, table, ident):
    c = _connect(root, True)
    try:
        ensure_schema(c)
        return _decode(_row(c, table, ident))
    finally:
        c.close()


def read_request(root, request_id):
    return _read_one(root, 'work_requests', request_id)


def read_return(root, return_id):
    return _read_one(root, 'work_returns', return_id)


def read_run(root, run_id):
    c = _connect(root, True)
    try:
        c.execute('BEGIN')
        ensure_schema(c)
        result = _run_view(c, _row(c, 'work_runs', run_id))
        events = [_decode(row) for row in c.execute('SELECT * FROM work_run_events WHERE run_id=? ORDER BY id', (run_id,))]
        result['events'] = events
        result['latest_receipt'] = next((row['receipt'] for row in reversed(events) if row.get('receipt')), None)
        return result
    finally:
        c.close()


def _run_view(c, row):
    view = _decode(row)
    registered = [r[0] for r in c.execute('SELECT id FROM work_returns WHERE run_id=? ORDER BY created_at,id', (row['id'],))]
    view['return_ids'] = registered
    view['delivery_status'] = ('returned' if registered else 'pending_delivery'
                               if row['outcome'] in ('succeeded', 'failed', 'stopped') else 'native_unresolved')
    latest = c.execute('SELECT receipt_json,payload_json FROM work_run_events WHERE run_id=? AND receipt_json IS NOT NULL ORDER BY id DESC LIMIT 1', (row['id'],)).fetchone()
    view['latest_receipt'] = json.loads(latest['receipt_json']) if latest else None
    view['execution_observation']=(json.loads(latest['payload_json']).get('execution_observation') if latest else None)
    view['identity_assurance'] = 'dispatcher_observed_native_receipt' if row['native_identity_json'] else 'launch_intent_only'
    return view


def _compact_snapshot(c, task, item_limit=5):
    requests = [_decode(row) for row in c.execute('SELECT * FROM work_requests WHERE task_id=? ORDER BY created_at DESC,id DESC LIMIT ?', (task['id'], item_limit))]
    runs = [_run_view(c, row) for row in c.execute("SELECT r.* FROM work_runs r JOIN work_requests q ON q.id=r.request_id WHERE q.task_id=? AND r.outcome IN ('intent','running','unknown') ORDER BY r.created_at,r.id", (task['id'],))]
    pending = _unresolved_requests(c, task)
    import harness_scope
    return {'task': _decode(task), 'requests': requests, 'unresolved_runs': runs[:item_limit],
            'pending_request_ids': pending[:item_limit],
            'scope':harness_scope.projection(c,workspace_root(c),task['id']),
            'counts': {'unreturned_or_open_requests': len(pending), 'unresolved_runs': len(runs),
                       'requests_total':c.execute('SELECT count(*) FROM work_requests WHERE task_id=?',(task['id'],)).fetchone()[0],
                       'requests_shown':len(requests),'unresolved_runs_shown':min(item_limit,len(runs)),'pending_requests_shown':min(item_limit,len(pending))},
            'detail': {'command': 'work-show', 'id': task['id']}, 'compact': True}


def current_view(root, actor=None, task_id=None, *, limit=20, offset=0, include_closed=False):
    """Generated role/task view; no mutable board, initialization or ledger."""
    path = Path(root).resolve() / '.control' / 'harness.sqlite'
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 100 or not isinstance(offset, int) or offset < 0:
        raise ValueError('Current task pagination requires limit 1..100 and nonnegative offset')
    if not path.is_file():
        return {'status': 'UNCONFIGURED', 'tasks': [], 'production_admission': False}
    c = _connect(root, True)
    try:
        c.execute('BEGIN')
        if not _exists(c, 'work_contract'):
            return {'status': 'UNCONFIGURED', 'tasks': [], 'legacy_database_preserved': True}
        ensure_schema(c)
        actor = verified_actor(root, actor) if actor is not None else None
        roles = _roles(c, actor) if actor else set()
        rows = [_row(c, 'work_tasks', task_id)] if task_id else c.execute('SELECT * FROM work_tasks ORDER BY updated_at DESC,id').fetchall()
        result = []
        eligible = []
        for task in rows:
            if actor and not participates(c, task, actor):
                continue
            if not task_id and not include_closed and task['state'] in ('completed', 'cancelled') :
                continue
            eligible.append(task)
        for task in eligible[offset:offset + limit]:
            result.append(_snapshot(c, task) if task_id else _compact_snapshot(c, task))
        return {'status': 'configured', 'version': VERSION, 'org_roles': sorted(roles),
                'tasks': result, 'generated': True, 'native_authority': False,
                'pagination': {'total': len(eligible), 'shown':len(result),'offset': offset, 'limit': limit,
                               'next_offset': offset + limit if offset + limit < len(eligible) else None,
                               'next_page_command':'python3 bin/harness.py work-list --offset '+str(offset+limit)+' --limit '+str(limit)+(' --all' if include_closed else '')+(' --mine' if actor else '') if offset+limit<len(eligible) else None}}
    finally:
        c.close()


def pending_decision(returned):
    return not returned.get('decisions') and not returned.get('consumed')


def task_entry(root, task_id):
    """Return original records without deriving the reader's next action."""
    import harness_snapshot
    return harness_snapshot.read(root, task_id=task_id)['work']


def save_note(root, home, label, text):
    from production_review import _save_fixed
    raw = _text(text, 'Original decision').encode('utf-8')
    return _save_fixed(root, Path(home)/(label+'-'+hashlib.sha256(raw).hexdigest()+'.md'),raw)


def _saved_document(root, path, *, home=None, label='judgment', allow_text=False):
    from platform_review import observation as business_board
    root = Path(root).resolve()
    if path == '-':
        if home is None:
            raise ValueError('Inline judgment needs its original request/package home')
        incoming = sys.stdin.buffer.read()
        try:
            document = json.loads(incoming)
        except ValueError:
            if not allow_text:
                raise
            document = {'contribution': incoming.decode('utf-8')}
        if not isinstance(document, dict):
            if not allow_text:
                raise ValueError('Inline judgment must be an object')
            document = {'contribution': incoming.decode('utf-8')}
        raw = incoming
        parent = Path(home).resolve()
        relative_parent = parent.relative_to(root)
        selected = business_board.safe_path(root, str(relative_parent / (label + '-' + hashlib.sha256(raw).hexdigest()[:24] + ('.md' if allow_text else '.json'))), must_exist=False)
        try:
            with selected.open('xb') as stream:
                stream.write(raw); stream.flush(); os.fsync(stream.fileno())
        except FileExistsError:
            if selected.read_bytes() != raw:
                raise ValueError('Original judgment path has different bytes')
        path = str(selected)
    relative = Path(path)
    if relative.is_absolute():
        relative = relative.relative_to(root)
    selected = business_board.safe_path(root, str(relative))
    if not selected.is_file():
        raise ValueError('Saved result/decision must be an existing JSON document')
    raw = selected.read_bytes()
    ref = evidence_ref(root, {'path': str(selected), 'sha256': hashlib.sha256(raw).hexdigest()})
    try:
        value = json.loads(raw)
    except ValueError:
        if not allow_text:
            raise
        value = {'contribution': raw.decode('utf-8')}
    if not isinstance(value, dict):
        if not allow_text:
            raise ValueError('This machine input requires a JSON object')
        value = {'contribution': raw.decode('utf-8')}
    return ref, value


def _contribution_artifact(root, artifact):
    from platform_review import observation as business_board
    if isinstance(artifact, str):
        artifact = {'path': artifact}
    if not isinstance(artifact, dict) or not artifact.get('path'):
        raise ValueError('Each artifact needs its original saved path')
    root = Path(root).resolve()
    path = Path(artifact['path'])
    if path.is_absolute():
        path = path.relative_to(root)
    path = business_board.safe_path(root, str(path))
    relative = str(path.relative_to(root))
    sha = hashlib.sha256(path.read_bytes()).hexdigest()
    if artifact.get('sha256', sha) != sha:
        raise ValueError(f'Artifact changed from its supplied digest: {relative}. Retain the original evidence.')
    return {'id': _text(artifact.get('id', relative), 'Artifact id'),
            'version': _text(artifact.get('version', sha), 'Artifact version'),
            'path': str(path), 'sha256': sha}


def contribution_payload(root, request, ref, document=None):
    """The report is the result. Its prose never becomes a management command."""
    raw = Path(evidence_ref(root, ref)['path']).read_text(encoding='utf-8')
    return {'request_id':request['id'], 'result_ref':ref,
            'artifact_refs':[_contribution_artifact(root, ref)],
            'coverage':[], 'remaining_scope':[],
            'summary':raw or 'Empty original report retained.'}


def validate_return_payload(root, request, payload):
    """Check supplied references; scope metadata is optional bookkeeping."""
    coverage=_list(payload.get('coverage', []),'Reported coverage')
    remaining=_list(payload.get('remaining_scope', []),'Reported remaining scope')
    evidence_ref(root,payload.get('result_ref'))
    _artifact_refs(root,payload.get('artifact_refs', []))
    _text(payload.get('summary'),'Actual contribution, changes and limits')


def end_delegation(root, actor, request_id, path):
    """End an exact assignment without erasing or resolving its native effects."""
    request = read_request(root, request_id)
    actor = verified_actor(root, actor)
    ref, _ = _saved_document(root, path, home=Path(request['input_ref']['path']).parent,
                             label='decision', allow_text=True)
    reason = _text(Path(ref['path']).read_text(encoding='utf-8'), 'Reason for ending the assignment')
    with transaction(root) as c:
        req = _row(c, 'work_requests', request_id)
        task = _row(c, 'work_tasks', req['task_id'])
        existing = _delegation_decisions(c, task['id']).get(request_id)
        if existing and existing['action'] == 'end':
            return existing
        event = {'request_id': request_id, 'return_id': None,
                 'decision_id': 'decision:' + hashlib.sha256(_json([request_id, actor_key(actor), ref]).encode()).hexdigest(),
                 'action': 'end', 'evidence_ref': ref, 'reason': reason}
        _event(c, task, 'delegation-decision', actor, event)
    return event


def resume_delegation(root, actor, request_id, instruction_ref):
    """Record the current owner's explicit resumption; retain the original end."""
    actor = verified_actor(root, actor)
    ref = evidence_ref(root, instruction_ref)
    reason = _text(Path(ref['path']).read_text(encoding='utf-8'), 'Original continuation instruction')
    with transaction(root) as c:
        req = _row(c, 'work_requests', request_id)
        task = _row(c, 'work_tasks', req['task_id'])
        _admit_scope(c, task)
        previous = _delegation_decisions(c, task['id']).get(request_id)
        if previous and previous['action'] == 'end':
            _event(c, task, 'delegation-decision', actor, {
                'request_id': request_id, 'action': 'resume', 'evidence_ref': ref,
                'reason': reason, 'previous_decision_id': previous['decision_id']})


def dispose_result(root, actor, return_id, path, *, disposition='hold',scope_ids=(),acceptance='stage',checks_path=None,apply_review_ids=(),handling_paths=(),wave=None):
    returned = _read_one(root, 'work_returns', return_id)
    request = read_request(root, returned['request_id'])
    ref, _ = _saved_document(root, path, home=Path(request['input_ref']['path']).parent,
                             label='decision', allow_text=True)
    reason = Path(ref['path']).read_text(encoding='utf-8')
    from task_ledger.sessions import reference
    handling=[reference(root,p) for p in handling_paths]
    ident = 'decision:' + hashlib.sha256(_json([return_id, native_key(actor), ref, disposition,list(scope_ids),acceptance,
        *([list(apply_review_ids),handling] if apply_review_ids or handling else [])]).encode()).hexdigest()
    return mutate(root, 'dispose', actor, {'id':ident, 'return_id':return_id,
        'disposition':disposition, 'summary':reason, 'evidence_ref':ref,'scope':list(scope_ids),
        'acceptance':acceptance,**({'checks_path':str(checks_path)} if checks_path else {}),
        **({'apply_review_return_ids':list(apply_review_ids),'handling_refs':handling} if apply_review_ids or handling else {}),
        **({'wave_bookmark':wave} if wave else {})})


def add_parsers(subparsers):
    q = subparsers.add_parser('work-capacity', help='Read host-specific author and review capacity')
    q.add_argument('--host', choices=('dsh', 'devin'), default='dsh', help='Select an independent host pool (default: dsh)')
    maintenance = q.add_mutually_exclusive_group()
    maintenance.add_argument('--adopt-policy',action='store_true',help='Maintainer: fence only proven never-started legacy transports in the original queue')
    maintenance.add_argument('--retire-exited-waits',action='store_true',help='Maintainer: remove only review-wait tickets whose original execution has exited')
    maintenance.add_argument('--provider-pilot',metavar='PROVIDER',help='Maintainer: four seats for one exact provider at an empty queue window')
    maintenance.add_argument('--end-provider-pilot',action='store_true',help='Maintainer: restore six-per-role policy after natural drain')
    q.add_argument('--source',help='Original authority for the selected maintenance action')
    q.add_argument('--dry-run',action='store_true')
    q.add_argument('--output')
    q = subparsers.add_parser('work-enroll', help='Record the current user appointment of this native session')
    q.add_argument('--authorization', required=True)
    q.add_argument('--role', choices=ORG_ROLES, required=True)
    q = subparsers.add_parser('work-accept', help='Acknowledge an existing exact assignment once; no result file')
    target = q.add_mutually_exclusive_group(required=True)
    target.add_argument('--request')
    target.add_argument('--task', help='Accept existing task ownership, without a mirror request')
    q = subparsers.add_parser('work-dispatch', help='Resolve existing recipient/runtime/recovery; prepare by default')
    target = q.add_mutually_exclusive_group(required=True)
    target.add_argument('--request')
    target.add_argument('--task', help='Prepare handoff to the existing exact task lead')
    q.add_argument('--config')
    q.add_argument('--execute', action='store_true', help='Start only a new unbound request through its qualified runtime')
    q.add_argument('--instructions', help='Original owner instruction file for the same terminal Session')
    q.add_argument('--basis', help='Original review return or revise decision for an already delivered author assignment')
    q.add_argument('--continue', dest='continue_original', action='store_true', help='Continue exact --run-id technically in its original Session, without a new instruction file')
    q.add_argument('--run-id', help='Exact original author or review attempt')
    q.add_argument('--resume-stopped', action='store_true', help='Explicitly resume the ended assignment or selected confirmed stopped Session with --instructions')
    q.add_argument('--goal-max-rounds',type=int,help='Owner-approved total for the exact exhausted DSH Goal')
    q = subparsers.add_parser('work-end', help='Explicitly end an exact assignment; adoption has no control effect')
    q.add_argument('--request', required=True)
    q.add_argument('--note', required=True)
    q = subparsers.add_parser('work-packages', help='Read original package, stage, run, session and adoption coordinates')
    q.add_argument('--task')
    q.add_argument('--output')
    q.add_argument('--limit',type=int);q.add_argument('--offset',type=int,default=0)
    q.add_argument('--detail',action='store_true')
    q = subparsers.add_parser('work-scope',help='Read the complete original scope or atomically apply explicit scope/relationship facts')
    q.add_argument('--task',required=True);q.add_argument('--apply');q.add_argument('--dry-run',action='store_true');q.add_argument('--output')
    q = subparsers.add_parser('work-contract',help='Read or set the original delivery contract')
    target=q.add_mutually_exclusive_group(required=True);target.add_argument('--task');target.add_argument('--request')
    q.add_argument('--set');q.add_argument('--source')
    q = subparsers.add_parser('work-check',help='Read-only dispatch, handoff or complete check of exact original evidence')
    target=q.add_mutually_exclusive_group(required=True);target.add_argument('--task');target.add_argument('--request')
    q.add_argument('--for',dest='for_action',required=True,choices=('dispatch','handoff','complete'))
    q.add_argument('--summary',action='store_true',help='Show exact candidates, original opinions and pending handoff checks')
    q = subparsers.add_parser('work-review-record',help='Index an explicitly stated original independent conclusion; no new review')
    q.add_argument('--supersedes',action='append',default=[],help='Exact prior review return explicitly resolved/replaced by this complete original opinion; repeatable')
    q.add_argument('--recurs-after',help='Refer an independently confirmed material recurrence using the earlier corrected opinion return ID')
    q.add_argument('--return-id',required=True);q.add_argument('--conclusion',required=True,choices=('supported','supported_with_limits','material_issue','not_assessed'))
    q.add_argument('--basis',required=True)
    q = subparsers.add_parser('work-submit-batch', help='Preview or execute exact accepted native review roots one at a time')
    q.add_argument('--task',required=True)
    q.add_argument('--input',help='Batch selection with roots referring to original adoptions and native input files')
    q.add_argument('--adoption',help='Original adoption ID for one root');q.add_argument('--request',help='Original native request file for one root')
    q.add_argument('--use',help='Original adoption file or line/JSON locator for a standalone Source or Flow')
    q.add_argument('--execute',action='store_true')
    q = subparsers.add_parser('work-maintenance', help='Generated unresolved maintenance findings across original tasks')
    q.add_argument('--limit', type=int, default=20)
    q.add_argument('--offset', type=int, default=0)
    q = subparsers.add_parser('work-maintenance-note', help='Record an explicit finding decision with its original note')
    q.add_argument('--request', required=True)
    q.add_argument('--note', required=True)
    q.add_argument('--status', required=True, choices=('open','fixed','verified','duplicate','deferred'))
    q.add_argument('--duplicate-of')
    q = subparsers.add_parser('work-maintenance-delegate', help='Record an explicit engineering assignment under the original finding')
    q.add_argument('--request', required=True, help='Original maintenance finding')
    q.add_argument('--brief', required=True, help='Original authorized repair brief, ordinary text')
    q.add_argument('--recipient', help='Actual existing recipient thread; omit for explicit native dispatch')
    q = subparsers.add_parser('work-claim-finding', help='Record responsibility for the original maintenance finding')
    q.add_argument('--request', required=True)
    q.add_argument('--authorization', help='Exact current human reassignment of an already assigned finding')
    q = subparsers.add_parser('work-finding', help='Report task friction before a normal return is possible; no remote Issue')
    q.add_argument('--task', required=True)
    q.add_argument('--report', required=True)
    q.add_argument('--finding', help='Append this observation to the exact original finding')
    q = subparsers.add_parser('work-init', help='Initialize the task store from an explicit user instruction')
    q.add_argument('--authorization', required=True)
    q = subparsers.add_parser('work-list', help='Generated current complete-task view; read-only')
    q.add_argument('--mine', action='store_true')
    q.add_argument('--limit', type=int, default=20)
    q.add_argument('--offset', type=int, default=0)
    q.add_argument('--all', action='store_true', help='Include settled closed tasks; history remains at work-show')
    q = subparsers.add_parser('work-show', help='Read one complete task or the exact frozen inputs of an original run')
    q.add_argument('--id', action='append')
    q.add_argument('--mine', action='store_true', help='Wait across current responsibilities of this native Session')
    views=q.add_mutually_exclusive_group()
    views.add_argument('--detail', action='store_true', help='Expand historical events and all internal records')
    views.add_argument('--scope', metavar='SCOPE_ID', help='Open one agreed scope item with its original authors, returns and dependencies')
    views.add_argument('--history', action='store_true', help='Page the original task events')
    views.add_argument('--wave', nargs='?', const='latest', metavar='DECISION_ID', help='Read a closed wave and its cutoff, or preview current wave facts')
    q.add_argument('--wait-seconds', type=int, help='Wait locally for a new actionable item, at most 1800 seconds')
    q.add_argument('--after', help='Resume reading after the last current-responsibility cursor')
    q.add_argument('--output')
    q.add_argument('--limit',type=int,default=10);q.add_argument('--offset',type=int,default=0)
    q = subparsers.add_parser('work-wait',help='Defer one current item until its original dependency changes or a stated time')
    q.add_argument('--task',required=True);q.add_argument('--item',required=True)
    dependency=q.add_mutually_exclusive_group()
    dependency.add_argument('--for-request',help='Exact prerequisite request')
    dependency.add_argument('--for-file',help='Original dependency evidence path')
    q.add_argument('--until',help='Recheck time including timezone')
    q.add_argument('--reason',help='Reason in the responsible person’s own words')
    q.add_argument('--external',action='store_true',help='This exact dependency blocks further core work')
    q = subparsers.add_parser('work-wave',help='Bookmark the facts at one existing original decision')
    q.add_argument('--task',required=True);q.add_argument('--decision',required=True)
    q = subparsers.add_parser('work-create',help='Register the original complete task')
    q.add_argument('--brief',required=True); q.add_argument('--owner'); q.add_argument('--id')
    q.add_argument('--contract')
    q = subparsers.add_parser('work-request',help='Assign an original complete work package')
    q.add_argument('--task',required=True); q.add_argument('--brief',required=True)
    q.add_argument('--review-of',help='Request independent review of this original assignment using the saved brief')
    q.add_argument('--continues',help='Explicitly retain the original author and scientific delivery lineage')
    q.add_argument('--contract');q.add_argument('--scope',action='append',default=[])
    for action in ('revise','reopen','pause','resume','cancel','complete','transfer'):
        q = subparsers.add_parser('work-'+action,help='Record an explicit task decision')
        q.add_argument('--task',required=True); q.add_argument('--note',required=True)
        if action == 'revise':
            q.add_argument('--base-sha256', required=True, help='Accepted baseline SHA-256 returned by work-show')
            why=q.add_mutually_exclusive_group(required=True)
            why.add_argument('--reason',help='Original wording of the change decision')
            why.add_argument('--reason-file',help='Saved original change decision')
            q.add_argument('--contract', help='Accept these exact changed delivery terms in the same baseline revision')
            q.add_argument('--affected-scope', action='append', default=[], help='Scope changed by this decision, repeat for each exact item')
            q.add_argument('--affected-request', action='append', default=[], help='Unsent request affected by this decision, repeat as needed')
            q.add_argument('--authorization', help='Original explicit user instruction authorizing this maintainer to edit this plan; preserves the lead and scientific ownership')
        if action == 'transfer': q.add_argument('--owner',required=True)
    q = subparsers.add_parser('work-return',help='Save the actual report or read back an original native run')
    q.add_argument('--request'); q.add_argument('--run'); q.add_argument('--result')
    q.add_argument('--scope',action='append',default=[])
    q.add_argument('--artifact',action='append',default=[],help='Associate this exact saved original file; generated identities/hashes, no hand-written status')
    q.add_argument('--candidate',action='append',default=[],help='Select the complete current candidate set by exact body or full native readback; repeat per dataset')
    q = subparsers.add_parser('work-dispose',help='Record the lead’s use of an original report')
    q.add_argument('--return-id',required=True); q.add_argument('--decision',required=True)
    q.add_argument('--use',required=True,choices=('adopt','revise','hold','reject'))
    q.add_argument('--scope',action='append',default=[]);q.add_argument('--acceptance',choices=('stage','final'),default='stage');q.add_argument('--checks')
    q.add_argument('--apply-review',action='append',default=[],help='Explicit task-lead acceptance of this original review after author handling for exact current candidates/use; repeatable')
    q.add_argument('--handling',action='append',default=[],help='Complete original author handling file supporting --apply-review; repeatable')
    q.add_argument('--wave',help='Original sponsor: cite the wave bookmark handled by this disposition')
    q = subparsers.add_parser('work-business-bind',help='Associate exact dataset subjects with a task')
    q.add_argument('--task',required=True); q.add_argument('--package',required=True)
    q = subparsers.add_parser('work-business-observe',help='Record an original native observation')
    q.add_argument('--task',required=True); q.add_argument('--capture',required=True)
    q.add_argument('--package-id'); q.add_argument('--expected-account-user-id')
