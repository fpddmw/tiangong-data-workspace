"""Index verified original decisions in the existing complete-task database."""
import contextlib
from datetime import datetime, timezone
import json
from pathlib import Path
import shlex
import re
import sys

import harness_tasks as tasks

FIELDS = ('task', 'package', 'object', 'action', 'account', 'project')
SCOPE_ARGS = tuple('--' + field for field in FIELDS)
DATASET_WRITE = 'dataset.remote_mutation'
DATASET_WRITE_GRANT = 'dataset.remote_mutation.authorization'
SCHEMA = (
    """CREATE TABLE IF NOT EXISTS work_decisions (
       id TEXT PRIMARY KEY, scope_json TEXT NOT NULL, subject TEXT NOT NULL,
       statement TEXT NOT NULL, source_ref_json TEXT NOT NULL, supersedes_json TEXT NOT NULL,
       effective_at TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('active','withdrawn','superseded')),
       recorded_by TEXT NOT NULL, recorded_at TEXT NOT NULL,
       withdrawal_ref_json TEXT, withdrawn_by TEXT, withdrawn_at TEXT)""",
)


def scope(value):
    if not isinstance(value, dict) or set(value) - set(FIELDS):
        raise ValueError('Decision scope uses exact task, package, object, action, account and project selectors')
    for key, selector in value.items():
        tasks._text(selector, 'Decision ' + key)
        if selector != selector.strip() or selector == '*':
            raise ValueError('Use exact selectors and omit a dimension for all its values')
    return dict(value)


def covers(outer, inner):
    """A partial assignment covers every matching, more specific assignment."""
    return all(inner.get(key) == value for key, value in outer.items())


def checked_source(root, ref):
    if not isinstance(ref, dict):
        raise ValueError('Use the original decision source with path, sha256, locator and decided_by')
    exact = tasks.evidence_ref(root, ref)
    path = Path(exact['path'])
    locator = tasks._text(ref.get('locator'), 'Original decision locator')
    decided_by = tasks._text(ref.get('decided_by'), 'Original decision maker')
    if locator == 'file':
        return {**exact, 'locator': locator, 'decided_by': decided_by}
    if locator.startswith('/') and path.suffix == '.json':
        try:
            value = json.loads(path.read_bytes())
            for part in locator[1:].split('/'):
                part = part.replace('~1', '/').replace('~0', '~')
                value = value[int(part)] if isinstance(value, list) and part.isascii() and part.isdecimal() else value[part]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise ValueError('Original decision JSON pointer does not resolve') from exc
    elif match := re.fullmatch(r'L([1-9][0-9]*)(?:-L([1-9][0-9]*))?', locator):
        start = int(match[1])
        end = int(match[2] or match[1])
        if start > end or end > len(path.read_text(encoding='utf-8').splitlines()):
            raise ValueError('Original decision line range does not resolve')
    else:
        raise ValueError('Use a JSON pointer, line range such as L7-L9, or file for a complete original decision')
    return {**exact, 'locator': locator, 'decided_by': decided_by}


def decode(row):
    result = tasks._decode(row)
    result.pop('withdrawal_ref_json', None)
    result.setdefault('details', {})
    for key in ('recorded_by', 'withdrawn_by'):
        if result.get(key):
            result[key] = json.loads(result[key])
    return result


def _effective_at(value):
    if value is None:
        return tasks._now()
    date = datetime.fromisoformat(tasks._text(value, 'Decision effective_at'))
    if date.tzinfo is None or date > datetime.now(timezone.utc):
        raise ValueError('Use the time of an already-effective original decision, including its timezone')
    return date.astimezone(timezone.utc).isoformat()


def _links(c, subject, selected, effective_at, entries):
    if not isinstance(entries, list):
        raise ValueError('Decision supersedes must be a list of original IDs and their replaced scopes')
    result = []
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError('Each supersession needs an original decision ID and exact replaced scope')
        original = decode(tasks._row(c, 'work_decisions', tasks._text(entry.get('id'), 'Superseded decision ID')))
        replaced = scope(entry.get('scope', selected))
        if (original['subject'] != subject or not covers(original['scope'], replaced)
                or not covers(selected, replaced) or original['effective_at'] > effective_at):
            raise ValueError('A supersession must stay within both decisions and the original subject and timeline')
        link = {'id': original['id'], 'scope': replaced}
        if link not in result:
            result.append(link)
    return sorted(result, key=tasks._json)


def dataset_write_authority(c, selected, actor):
    from task_ledger.finite_work import _package_id, request_configs
    from harness_scope import _owner
    from dataset_checks.references import UUID, VERSION

    parts = selected.get('object', '').split('/')
    if (set(selected) != set(FIELDS) or len(parts) != 3 or not UUID.fullmatch(parts[1])
            or parts[1] != parts[1].lower() or not VERSION.fullmatch(parts[2])
            or selected['action'] not in ('create', 'save', 'delete', 'result-group')):
        raise ValueError('Use an exact task, original package request, type/UUID/version, mutation action, account and project.')
    task = tasks._decode(tasks._row(c, 'work_tasks', selected['task']))
    request = tasks._decode(tasks._row(c, 'work_requests', selected['package']))
    if request['task_id'] != task['id'] or _package_id(request, request_configs(c, task['id'])) != request['id']:
        raise ValueError('Select the original package request of this task.')
    _owner(c, task, actor, request=request)


def _add(c, root, document, actor, *, restoring=False, internal=False):
    if not isinstance(document, dict):
        raise ValueError('Decision input must be an object')
    if set(document) - {'id', 'scope', 'subject', 'statement', 'source_ref', 'supersedes', 'effective_at', 'details'}:
        raise ValueError('Decision input contains unsupported fields')
    ident = tasks._text(document.get('id'), 'Decision ID')
    selected = scope(document.get('scope'))
    subject = tasks._text(document.get('subject'), 'Decision subject')
    statement = tasks._text(document.get('statement'), 'Decision statement').strip()
    if subject in (DATASET_WRITE, DATASET_WRITE_GRANT):
        dataset_write_authority(c, selected, actor)
        if subject == DATASET_WRITE and statement not in ('stop', 'release'):
            raise ValueError('Dataset mutation control must say stop or release.')
        if subject == DATASET_WRITE_GRANT and statement != 'allow':
            raise ValueError('Index the original exact mutation authorization as allow.')
    key = tasks.actor_key(actor)
    source = checked_source(root, document.get('source_ref'))
    tasks.add_schema(c, SCHEMA, ('work_decisions',))
    if 'details_json' not in {row[1] for row in c.execute('PRAGMA table_info(work_decisions)')}:
        c.execute("ALTER TABLE work_decisions ADD COLUMN details_json TEXT NOT NULL DEFAULT '{}'")
    existing = c.execute('SELECT * FROM work_decisions WHERE id=?', (ident,)).fetchone()
    import harness_wave as wave
    from platform_review.submission import APPROVE,INDEPENDENT_USE
    from scientific_review.rounds import EXTRA
    retired=(wave.DEPENDENCY,wave.READY,wave.WAIT,wave.WAVE,wave.WAVE_CHECK,wave.INTAKE,wave.PRIORITY,APPROVE,INDEPENDENT_USE,EXTRA)
    if subject in retired and not (existing or internal):
        raise ValueError('This original decision remains readable. Use the current work entry for a new action.')
    effective = _effective_at(document.get('effective_at')) if not existing or 'effective_at' in document else existing['effective_at']
    links = _links(c, subject, selected, effective, document.get('supersedes', []))
    intake = None
    if isinstance(document.get('details'), dict) and document['details'].get('intake_id'):
        from task_ledger import role_intake
        intake = role_intake.admit(c, root, actor, document)
    details = ({key: document['details'][key] for key in ('intake_id','matter')} if intake else
               wave.decision_details(c, root, {**document, 'scope': selected, 'statement': statement}, actor))
    if existing and subject == wave.WAIT:
        details['dependency_versions'] = json.loads(existing['details_json']).get('dependency_versions', {})
    if not existing and subject == wave.WAIT:
        prior = wave._selected(c, root, selected, wave.WAIT)
        if prior and prior['scope'] == selected and prior['statement'] == statement and prior['details'] == details:
            return prior
    fields = {'scope_json': tasks._json(selected), 'subject': subject, 'statement': statement,
              'source_ref_json': tasks._json(source), 'supersedes_json': tasks._json(links),
              'effective_at': effective, 'recorded_by': key, 'details_json': tasks._json(details)}
    if existing:
        previous = decode(existing)['source_ref']
        comparable = {**fields, 'recorded_by': existing['recorded_by'],
                      'source_ref_json': tasks._json({k: v for k, v in source.items() if k != 'binding'})}
        for field, value in comparable.items():
            before = tasks._json({k: v for k, v in previous.items() if k != 'binding'}) if field == 'source_ref_json' else existing[field]
            if before != value:
                raise ValueError('Decision ID already indexes a different original record')
        if restoring and source.get('binding') and not previous.get('binding'):
            c.execute('UPDATE work_decisions SET source_ref_json=? WHERE id=?',
                      (fields['source_ref_json'], ident))
            return decode(tasks._row(c, 'work_decisions', ident))
        return decode(existing)
    c.execute("""INSERT INTO work_decisions
                 (id,scope_json,subject,statement,source_ref_json,supersedes_json,effective_at,recorded_by,details_json,status,recorded_at)
                 VALUES (?,?,?,?,?,?,?,?,?,'active',?)""", (ident, *fields.values(), tasks._now()))
    for link in links:
        c.execute("""UPDATE work_decisions SET status='superseded'
                     WHERE id=? AND scope_json=? AND status='active'""",
                  (link['id'], tasks._json(link['scope'])))
    result = decode(tasks._row(c, 'work_decisions', ident))
    if subject == wave.WAVE:
        wave.close(c, root, actor, result)
    if intake:
        role_intake.finished(c, selected['task'], actor, intake, result)
    return result


def add(root, document, *, actor=None):
    """Index one original decision or an atomic restoration of existing sources."""
    actor = tasks.verified_actor(root, actor)
    with tasks.transaction(root) as c:
        if isinstance(document, dict) and 'records' in document:
            records = document['records']
            if not isinstance(records, list) or not records:
                raise ValueError('Restoration requires exact original decision records')
            restored = [_add(c, root, item, actor, restoring=True) for item in records]
            return {'status': 'recorded', 'count': len(restored), 'records': restored}
        return _add(c, root, document, actor)


def withdraw(root, ident, source_ref, *, actor=None):
    """Retain the original and withdrawal references; superseded rules stay retired."""
    actor = tasks.verified_actor(root, actor)
    with tasks.transaction(root) as c:
        if not tasks._exists(c, 'work_decisions'):
            raise ValueError('The original decision index is unconfigured')
        original = decode(tasks._row(c, 'work_decisions', ident))
        if original['subject'] in (DATASET_WRITE, DATASET_WRITE_GRANT):
            dataset_write_authority(c, original['scope'], actor)
        key = tasks.actor_key(actor)
        ref = checked_source(root, source_ref)
        if original['status'] == 'withdrawn':
            if original['withdrawal_ref'] != ref or tasks.actor_key(original['withdrawn_by']) != key:
                raise ValueError('The decision already retains its original withdrawal')
            return original
        if original['status'] != 'active':
            raise ValueError('The original decision is already fully superseded')
        c.execute("""UPDATE work_decisions SET status='withdrawn', withdrawal_ref_json=?,
                     withdrawn_by=?, withdrawn_at=? WHERE id=?""", (tasks._json(ref), key, tasks._now(), ident))
        return decode(tasks._row(c, 'work_decisions', ident))


def _read(root, query, values=()):
    path = Path(root) / '.control/harness.sqlite'
    if not path.is_file():
        return None
    with contextlib.closing(tasks._connect(root, True, timeout=1)) as c:
        if not tasks._exists(c, 'work_decisions'):
            return None
        tasks.ensure_schema(c)
        return [decode(row) for row in c.execute(query, values)]


def get(root, ident):
    rows = _read(root, 'SELECT * FROM work_decisions WHERE id=?', (ident,))
    if not rows:
        raise ValueError('Original decision not found: ' + ident)
    return rows[0]


def detail_command(selected):
    args = (' ' + flag + ' ' + shlex.quote(selected[key])
            for key, flag in zip(FIELDS, SCOPE_ARGS) if key in selected)
    return 'python3 bin/harness.py work-decisions' + ''.join(args)


def _selected_claims(root, candidates, selected, *, verify_sources=True):
    maximal = [item for item in candidates if not any(
        item['scope'] != other['scope'] and covers(item['scope'], other['scope']) for other in candidates)]
    items, claims = [], set()
    for row in maximal:
        item = {key: row[key] for key in ('id', 'scope', 'statement', 'source_ref', 'recorded_by', 'details')}
        binding = row['source_ref'].get('binding')
        item['consumption'] = 'documentary'
        try:
            item['source_ref'] = checked_source(root, row['source_ref']) if verify_sources else row['source_ref']
            if binding:
                item['source_ref']['binding'] = binding
            claims.add(row['statement'])
        except (ValueError, OSError) as exc:
            item['source_issue'] = str(exc)
        items.append(item)
    state = 'source_unverified' if any('source_issue' in item for item in items) else 'conflict' if len(claims) > 1 else 'applicable'
    return state, items


def resolve(root, selected=None, *, connection=None, subject=None, as_of=None,verify_sources=True):
    """Resolve current or originally bound clauses using the same scope rules."""
    selected = scope({} if selected is None else selected)
    query = 'SELECT * FROM work_decisions'
    values = (subject,) if subject is not None else ()
    if values:
        query += ' WHERE subject=?'
    query += ' ORDER BY subject, effective_at, id'
    if connection is None:
        rows = _read(root, query, values)
    else:
        rows = [decode(row) for row in connection.execute(query, values)] if tasks._exists(connection, 'work_decisions') else None
    result = {'scope': selected, 'authority': 'Original source index. Read the original instructions and current native authority; this index never selects a runtime.',
              'detail_command': detail_command(selected), 'subjects': []}
    if rows is None:
        return {**result, 'status': 'unconfigured'}
    if as_of is not None:
        rows = [row for row in rows if row['effective_at'] <= as_of
                and row['source_ref'].get('binding', {}).get('original_event_at', row['recorded_at']) <= as_of]
    groups = {}
    narrower = set()
    replaced = {link['id'] for row in rows for link in row['supersedes'] if covers(link['scope'], selected)}
    for row in rows:
        if as_of is None:
            active = row['status'] == 'active'
        else:
            active = not row.get('withdrawn_at') or row['withdrawn_at'] > as_of
        if not active or row['id'] in replaced:
            continue
        if covers(row['scope'], selected):
            groups.setdefault(row['subject'], []).append(row)
        elif selected and all(row['scope'].get(key, value) == value for key, value in selected.items()):
            narrower.add(tasks._json({**selected, **row['scope']}))
    if narrower:
        result['narrower_scopes'] = [{'scope': more, 'detail_command': detail_command(more)}
            for value in sorted(narrower) if (more := json.loads(value))]
    for name, candidates in groups.items():
        status, notes = _selected_claims(root, candidates, selected,verify_sources=verify_sources)
        value = {'subject':name, 'status':status, 'decisions':notes}
        result['subjects'].append(value)
    return {**result, 'status': 'configured'}


def add_scope_args(parser):
    for field, flag in zip(FIELDS, SCOPE_ARGS):
        parser.add_argument(flag, dest='decision_' + field, help='Exact decision scope ' + field)


def scope_args(args):
    return {field: value for field in FIELDS if (value := getattr(args, 'decision_' + field, None)) is not None}


def add_parsers(parsers):
    cmd = parsers.add_parser('work-decisions', help='Resolve current original decisions for an exact scope')
    add_scope_args(cmd)
    show = parsers.add_parser('work-decision-show', help='Read one indexed original decision and its history')
    show.add_argument('--id', required=True)
    record = parsers.add_parser('work-decision-record', help='Index a reviewed original decision')
    target=record.add_mutually_exclusive_group(required=True)
    target.add_argument('--decision', help='One decision or an original records batch in saved JSON; use - for stdin')
    target.add_argument('--review', help='Original author request or review for an additional review investment')
    record.add_argument('--add',type=int,help='Additional rounds for --review')
    record.add_argument('--reason',help='Original reason for the additional review')
    record.add_argument('--object',help='Exact type/UUID when the review covers multiple objects')
    revoke = parsers.add_parser('work-decision-withdraw', help='Index the original withdrawal of a decision')
    revoke.add_argument('--id', required=True)
    revoke.add_argument('--source', required=True, help='Original withdrawal source reference JSON or - for stdin')


def _document(root, path):
    if path != '-':
        return tasks._saved_document(root, path)[1]
    return json.load(sys.stdin.buffer)


def dispatch(root, args):
    if args.command == 'work-decisions':
        return resolve(root, scope_args(args))
    if args.command == 'work-decision-show':
        return get(root, args.id)
    if args.command == 'work-decision-record':
        if args.review:
            from scientific_review.rounds import authorize
            return authorize(root,args.review,args.add,args.reason,args.object)
        if any(value is not None for value in (args.add,args.reason,args.object)):
            raise ValueError('Use --add, --reason and optional --object only with --review')
        return add(root, _document(root, args.decision))
    if args.command == 'work-decision-withdraw':
        return withdraw(root, args.id, _document(root, args.source))
    return None
