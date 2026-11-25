#!/usr/bin/env python3
"""Retain observed tool failures; workspace files own agent instructions.

Hook receipts are immutable source evidence, not another task/ownership store.
Only the original harness SQLite owns findings and their resolution. No prompt,
command body, tool output or chat history is copied into these receipts.
"""
import argparse
import contextlib
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import sqlite3
import subprocess
import sys
import tempfile

import harness_tasks as tasks
from task_ledger import sessions

ROOT = Path(__file__).resolve().parent.parent
SCHEMA = 'data-workspace.tool-observation.v1'
UNKNOWN_RUNTIME = {'config_sha256': 'unknown', 'reported_tool_version': 'unknown'}


def digest(value):
    return hashlib.sha256(tasks._json(value).encode()).hexdigest()


def event_directory(event):
    cwd = str(event.get('cwd', ''))
    if cwd.startswith('\\\\'):
        parts = cwd.replace('\\', '/').split('/')
        cwd = '/' + '/'.join(parts[4:]) if len(parts) > 4 and parts[2].lower() in ('wsl$', 'wsl.localhost') else ''
    return Path(cwd).absolute() if cwd else None


def actor_for(root, event):
    actor = tasks.verified_actor(root)
    # Subagent events can name their parent. Never register that parent as the
    # writer of a child's observation. Managed children also have native receipts.
    if event.get('session_id') != actor['id'] or event.get('agent_id'):
        raise ValueError('Hook identity is not this exact native session; no parent identity fallback')
    cwd = event_directory(event)
    if cwd is None or not cwd.resolve().is_relative_to(root):
        raise ValueError('Hook event belongs to a different workspace')
    return actor


def tool_command(event):
    tool_input = event.get('tool_input')
    if not isinstance(tool_input, dict):
        return ''
    cmd = tool_input.get('cmd')
    return cmd if isinstance(cmd, str) and cmd else tool_input.get('command', '')


def failed_tool(event):
    """Recognize explicit failure facts, never classify prose as an engineering bug."""
    response = event.get('tool_response')
    if isinstance(response, str):
        try:
            response = json.loads(response)
        except ValueError:
            match = re.search(r'(?:Process exited with code|Exit code:)\s*(-?\d+)', response)
            response = {'exit_code': int(match[1])} if match else {}
    if not isinstance(response, dict):
        return None
    code = response.get('exit_code', response.get('exitCode', response.get('returncode')))
    command = tool_command(event)
    # The documented no-match status of an ordinary search is not a tool failure.
    if code == 1 and isinstance(command, str) and re.match(r'^\s*(?:rg|grep)\s', command) and not re.search(r'[;&|\n]', command):
        return None
    if isinstance(code, int) and not isinstance(code, bool) and code != 0:
        return {'kind': 'nonzero_exit', 'exit_code': code}
    if response.get('isError') is True or response.get('is_error') is True:
        return {'kind': 'tool_error'}
    if response.get('error') and not response.get('session_id'):
        return {'kind': 'tool_error'}
    return None


PUBLIC_COMPONENTS = frozenset({
    'bin/agent_runtime.py', 'bin/context.py', 'bin/env_run.py', 'bin/harness.py',
    'bin/lca_gate.py', 'bin/launch.py', 'bin/run_worker.py', 'tiangong-lca', 'tidas', 'snapshot_builder',
})
GENERIC_TOOLS = frozenset({'unknown', 'Bash', 'bash', 'shell', 'exec_command', 'commandExecution',
                          'functions.exec_command', 'functions.exec', 'mcpToolCall', 'dynamicToolCall', 'fileChange', 'native_turn'})


def known_failure_component(value):
    return isinstance(value, str) and value in PUBLIC_COMPONENTS


def failure_component(command, root=ROOT):
    """Read only a fixed public executable name from a simple invocation."""
    if not isinstance(command, str) or len(command) > 8192 or any(c in command for c in ';|&`$\n<>'):
        return None
    try:
        argv = shlex.split(command)
    except ValueError:
        return None
    if not argv:
        return None
    program = Path(argv[0]).name
    if program in ('python', 'python3'):
        operands = argv[1:]
        if operands and operands[0] == '-B':
            operands = operands[1:]
        if operands:
            script = Path(operands[0])
            if script.is_absolute():
                try:
                    script = script.relative_to(Path(root).resolve())
                except ValueError:
                    return None
            program = str(script)  # Only fixed names under the exact workspace are allowlisted.
    return program if known_failure_component(program) else None


def candidates(root, actor):
    """Freeze exact responsibility at observation time, including paused owners."""
    result = {}
    offset = 0
    while True:
        view = sessions.overview(root, actor, limit=100, offset=offset, assignments_only=True, timeout=1)
        key = tasks.actor_key(actor)
        for row in view['tasks']:
            native = {r['request_id'] for r in row['runs']
                      if r.get('native_identity') and tasks.actor_key(r['native_identity']) == key}
            requests = [r['id'] for r in row['requests'] if r['id'] in native
                        or r.get('recipient_actor') and tasks.actor_key(r['recipient_actor']) == key]
            if requests or tasks.actor_key(row['task']['owner_actor']) == key:
                result[row['task']['id']] = requests
        offset = view['pagination']['next_offset']
        if offset is None:
            break
    return result


def observation_home(root):
    base = (root / '.harness-local' / 'host-observations').resolve()
    if not base.is_relative_to(root.resolve()):
        raise ValueError('Tool observation path escapes the original workspace')
    return base


def receipt_home(root, actor):
    # Opaque native IDs never become filesystem paths.
    base = observation_home(root)
    home = (base / digest(actor)[:24]).resolve()
    if not home.is_relative_to(base):
        raise ValueError('Tool observation path escapes the original receipt directory')
    return home


def runtime_binding(root, event):
    binding = dict(UNKNOWN_RUNTIME)
    # This identifies the configured launch binding. Actual installed tool
    # version remains unknown unless the original event carries it explicitly.
    path = root / '.control' / 'runtime.json'
    try:
        if (not path.is_symlink() and path.resolve().is_relative_to(root.resolve())
                and path.is_file() and path.stat().st_size <= 1024 * 1024):
            binding['config_sha256'] = hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        pass
    version = event.get('tool_version')
    if isinstance(version, str) and re.fullmatch(r'v?[0-9]+(?:\.[0-9]+){1,3}(?:[-+][A-Za-z0-9.-]{1,30})?', version):
        binding['reported_tool_version'] = version
    return binding


def record_failure(root, actor, event):
    failure = failed_tool(event)
    if not failure:
        return None
    call_id = event.get('tool_use_id')
    if not isinstance(call_id, str) or not call_id:
        raise ValueError('Failed tool event lacks its original invocation ID')
    try:
        bound = candidates(root, actor)
    except (ValueError, OSError, sqlite3.Error):
        bound = {}
    tool = event.get('tool_name', 'unknown')
    tool = tool if isinstance(tool, str) and re.fullmatch(r'[A-Za-z0-9_./:-]{1,160}', tool) else 'unknown'
    component = failure_component(tool_command(event), root)
    document = {'actor': actor,
        'tool_name': tool, 'tool_use_id': call_id, 'turn_id': event.get('turn_id'),
        'observed_at': tasks._now(), 'failure': failure, 'task_candidates': bound,
        'runtime_binding': runtime_binding(root, event),
        **({'component': component} if component else {}),
        'input_sha256': digest(event.get('tool_input')), 'response_sha256': digest(event.get('tool_response')),
        'summary': 'Observed ' + tool + ' ' + tasks._json(failure) + '; cause untriaged. Recover original native tool call ' + call_id,
        'authority': 'Observed failure only; no scientific coverage, repair assignment or replay permission.'}
    return retain_observation(root, document)


def retain_observation(root, document):
    """Publish an already verified hook event or an original native receipt projection."""
    actor = document['actor']
    ident = digest([actor, document.get('turn_id'), document['tool_use_id']])
    document = {'schema': SCHEMA, 'id': ident, **document}
    home = receipt_home(root, actor)
    home.mkdir(parents=True, exist_ok=True)
    path = home / (ident + '.json')
    if path.is_symlink():
        raise ValueError('Original tool observation cannot be a symbolic link')
    if path.exists():
        existing = json.loads(path.read_bytes())
        if existing.get('response_sha256') != document['response_sha256']:
            raise ValueError('Original tool invocation has conflicting failure observations')
        return path
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=home, delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(tasks._json(document) + '\n')
            stream.flush()
            os.fsync(stream.fileno())
        # Atomic no-clobber publication: readers never see half a JSON receipt.
        os.link(temporary, path)
    except FileExistsError:
        return retain_observation(root, document)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return path


def read_observation(root, path):
    path = Path(path)
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 256 * 1024:
        raise ValueError('Use an exact bounded original hook receipt')
    raw = path.read_bytes()
    doc = json.loads(raw)
    if not isinstance(doc, dict) or doc.get('schema') != SCHEMA or not isinstance(doc.get('actor'), dict):
        raise ValueError('Tool observation must be an original event object')
    bound = doc.get('task_candidates')
    if (not isinstance(bound, dict) or not all(isinstance(value, list) and all(isinstance(r, str) and r for r in value) for value in bound.values())
            or not isinstance(doc.get('failure'), dict)
            or not isinstance(doc.get('runtime_binding', UNKNOWN_RUNTIME), dict)
            or not all(isinstance(doc.get(key), str) and doc[key] for key in
                       ('tool_name', 'tool_use_id', 'input_sha256', 'summary'))):
        raise ValueError('Original tool receipt lacks exact failure and task-routing facts')
    source = doc.get('native_source')
    if source is not None and (not isinstance(source, dict) or source.get('kind') != 'native_run_receipt'
                               or not isinstance(source.get('receipt'), dict)
                               or not isinstance(source.get('run_id'), str)):
        raise ValueError('Tool observation lacks its exact original native run receipt')
    actor = tasks._actor(root, doc['actor'], observed=source is not None)
    ident = digest([actor, doc.get('turn_id'), doc.get('tool_use_id')])
    if path.resolve() != receipt_home(root, actor) / (ident + '.json') or doc.get('id') != ident:
        raise ValueError('Tool observation must retain its actual original actor and invocation')
    return {'path': str(path.resolve()), 'sha256': hashlib.sha256(raw).hexdigest()}, doc


def observation_signature(doc):
    binding = doc.get('runtime_binding', UNKNOWN_RUNTIME)
    if binding.get('profile_sha256'):
        binding = {**binding, 'config_sha256': binding['profile_sha256']}
    return digest([doc['task_candidates'], doc['tool_name'], doc['input_sha256'], doc['failure'],
                   binding])


def common_problem(doc):
    """Candidate similarity uses a known component and comparable runtime evidence."""
    component = doc.get('component')
    if not known_failure_component(component):
        tool = doc['tool_name']
        component = tool if tool not in GENERIC_TOOLS and re.fullmatch(r'[A-Za-z0-9_./:-]{1,160}', tool) else None
    binding = doc.get('runtime_binding', UNKNOWN_RUNTIME)
    runtime = {key: binding[key] for key in ('profile_sha256', 'reported_tool_version')
               if isinstance(binding.get(key), str) and binding[key] != 'unknown'}
    if 'profile_sha256' not in runtime and isinstance(binding.get('config_sha256'), str) and binding['config_sha256'] != 'unknown':
        runtime['config_sha256'] = binding['config_sha256']
    if not component or not runtime:
        return None
    symptom = {key: doc['failure'][key] for key in ('kind', 'exit_code') if key in doc['failure']}
    return {'component': component, 'runtime': runtime, 'symptom': symptom}


def affected_request(doc, task_id):
    result = {'task_id': task_id, 'request_ids': doc['task_candidates'].get(task_id, [])}
    if source := doc.get('native_source'):
        result['run_id'] = source['run_id']
        result['original_run_command'] = shlex.join(
            ['python3', 'bin/agent_runtime.py', 'readresult', '--run-id', source['run_id']])
    if result['request_ids']:
        result['original_request_commands'] = [
            shlex.join(['python3', 'bin/context.py', '--request', req]) for req in result['request_ids']]
    else:
        result['original_task_command'] = shlex.join(['python3', 'bin/context.py', '--task', task_id])
    return result


def observation_rows(root, *, home=None, issues=None):
    base = observation_home(root) if home is None else home
    paths = base.glob('*/*.json' if home is None else '*.json')
    for path in sorted(paths):
        try:
            yield read_observation(root, path)
        except (ValueError, OSError) as exc:
            if issues is not None:
                issues.append({'status': 'unavailable', 'receipt': str(path), 'reason': str(exc)})


def consumed_observations(c, confirmed=None, original_findings=None):
    paths = set()
    for row in c.execute("SELECT task_id,id,input_ref_json FROM work_requests WHERE id LIKE 'tool-friction:%'"):
        path = json.loads(row[2])['path']
        paths.add(path)
        if original_findings is not None:
            original_findings[path] = (row[0], row[1])
    for row in c.execute("SELECT task_id,payload_json FROM work_task_events WHERE kind IN ('maintenance-observation','maintenance-observation-triaged')"):
        event = json.loads(row[1])
        if confirmed is not None and event.get('problem_id') and event.get('request_id'):
            confirmed.setdefault(event['problem_id'], set()).add((row[0], event['request_id']))
        if ref := event.get('result_ref'):
            paths.add(ref['path'])
        paths.update(ref['path'] for ref in event.get('occurrence_refs', []))
    return paths


def pending_observations(root, actor, *, all_actors=False, connection=None, problem_id=None):
    """Read exact observations and compare possible common problems for maintenance."""
    actor = actor if all_actors else tasks.verified_actor(root, actor)
    if problem_id and not all_actors:
        raise ValueError('A common problem view requires maintainer --all scope')
    routing_issue = None
    confirmed, original_findings = {}, {}
    try:
        with (contextlib.nullcontext(connection) if connection is not None else
              contextlib.closing(tasks._connect(root, True, timeout=1))) as c:
            tasks.ensure_schema(c)
            linked = consumed_observations(c, confirmed if all_actors else None, original_findings if all_actors else None)
    except (ValueError, OSError, sqlite3.Error) as exc:
        if all_actors:
            raise
        linked = set()
        routing_issue = str(exc)
    exact, issues = {}, []
    for ref, doc in observation_rows(root, home=None if all_actors else receipt_home(root, actor), issues=issues):
        problem = common_problem(doc) if all_actors else None
        key = 'tool-problem:' + digest(problem) if problem else None
        if ref['path'] in linked:
            if key and ref['path'] in original_findings:
                confirmed.setdefault(key, set()).add(original_findings[ref['path']])
            continue
        if problem_id and key != problem_id:
            continue
        signature = doc['id'] if problem_id else observation_signature(doc)
        bound = doc['task_candidates']
        route = (' --task ' + shlex.quote(next(iter(bound)))) if len(bound) == 1 else ' --task EXACT_ORIGINAL_TASK'
        source = doc.get('native_source')
        if source:
            source = {**source, 'recovery_command': shlex.join(
                ['python3', 'bin/agent_runtime.py', 'readresult', '--run-id', source['run_id']])}
        row = exact.setdefault(signature, {
            'status': 'observed', 'source_ref': ref, 'candidates': list(bound),
            'summary': doc['summary'], 'occurrences': 0,
            'runtime_binding': doc.get('runtime_binding', UNKNOWN_RUNTIME),
            **({'native_source': source} if source else {}),
            **({'link_command': 'python3 bin/harness_hook.py link --receipt ' + shlex.quote(ref['path']) + route}
               if all_actors else {}),
            **({'affected': [affected_request(doc, task_id) for task_id in bound]} if all_actors else {}),
            **({'problem_id': key, 'problem': problem} if key else {}),
            **({'routing_issue': routing_issue} if routing_issue else {})})
        row['occurrences'] += 1
    if problem_id:
        findings=[{'task_id':task,'request_id':finding} for task,finding in sorted(confirmed.get(problem_id,[]))]
        for row in exact.values():
            row.update(affected_total=len(row.get('affected',[])),affected_shown=len(row.get('affected',[])),
                existing_findings=findings,existing_findings_total=len(findings),existing_findings_shown=len(findings))
        return list(exact.values())
    if not all_actors:
        return [*exact.values(), *issues]
    groups = {}
    for signature, row in exact.items():
        groups.setdefault(row.get('problem_id', signature), []).append(row)
    output = []
    for key, rows in groups.items():
        first = rows[0]
        if not first.get('problem_id'):
            first.update(affected_total=len(first.get('affected',[])),affected_shown=len(first.get('affected',[])),
                         existing_findings_total=0,existing_findings_shown=0)
            output.append(first)
            continue
        # Retain one exact route at top for existing clients. Multiple exact
        # observations stay distinct, and the detail page exposes every route.
        affected=list({tasks._json(target):target for row in rows for target in row['affected']}.values())
        findings=[{'task_id':task,'request_id':finding} for task,finding in sorted(confirmed.get(key,[]))]
        output.append({**first, 'occurrences': sum(row['occurrences'] for row in rows),
            'candidates': list(dict.fromkeys(task for row in rows for task in row['candidates'])),
            'affected': affected[:8],'affected_total':len(affected),'affected_shown':min(8,len(affected)),
            'exact_group_count': len(rows),
            'existing_findings':findings[:8],'existing_findings_total':len(findings),'existing_findings_shown':min(8,len(findings)),
            'problem_detail_command': shlex.join(['python3', 'bin/harness_hook.py', 'pending', '--all', '--problem', key]),
            'problem_state': 'candidate_similarity_requires_confirmation'})
    return [*output, *issues]


def triage_page(rows, *, limit, offset=0, problem_id=None):
    if not 1 <= limit <= 100 or offset < 0:
        raise ValueError('Observation pagination needs a limit of 1..100 and nonnegative offset')
    next_offset = offset + limit if offset + limit < len(rows) else None
    command = ['python3', 'bin/harness_hook.py', 'pending', '--all']
    if problem_id:
        command.extend(['--problem', problem_id])
    return {'status': 'configured', 'items': rows[offset:offset + limit],
            'total': len(rows), 'offset': offset, 'next_offset': next_offset,
            'next_page_command': shlex.join([*command, '--limit', str(limit), '--offset', str(next_offset)])
                                 if next_offset is not None else None,
            'triage': 'Compare original observations to confirm a common cause. Use an exact link with --finding ID to reuse an existing finding, or --dismiss REASON for an invocation correction.'}


def link_observation(root, actor, path, *, task_id=None, finding_id=None, dismiss=None):
    """Record the maintainer's triage in the original work task."""
    actor = tasks.verified_actor(root, actor)
    if dismiss is not None:
        tasks._text(dismiss, 'Observation triage reason')
    ref, doc = read_observation(root, path)
    bound = doc['task_candidates']
    if not task_id and len(bound) == 1:
        task_id = next(iter(bound))
    if not task_id:
        return {'status': 'needs_task', 'source_ref': ref, 'candidates': list(bound)}
    if bound and task_id not in bound:
        raise ValueError('Select an original task retained with the observation')
    if finding_id and dismiss:
        raise ValueError('Choose an existing finding or a disposition without a finding')
    signature = observation_signature(doc)
    # An exact single-task association can carry repeated matching receipts.
    # Ambiguous/unbound observations are routed by the maintainer one at a time.
    matches = ([(r, row) for r, row in observation_rows(root) if observation_signature(row) == signature]
               if len(bound) == 1 else [(ref, doc)])
    with tasks.transaction(root, timeout=1) as c:
        actor = tasks.verified_actor(root, actor)
        task = tasks._row(c, 'work_tasks', task_id)
        consumed = consumed_observations(c)
        fresh = [r for r, _ in matches if r['path'] not in consumed]
        if not fresh:
            return {'status': 'already_triaged', 'source_ref': ref, 'task_id': task_id}
        ident, finding_task = finding_id, task
        if not dismiss:
            if ident is None:
                # Preserve the deterministic identity already used for exact
                # repeats, including its older runtime-unknown form.
                ident = 'tool-friction:' + digest([task_id, signature])
                legacy_ids = ['tool-friction:' + digest([task_id, row['actor'], bound.get(task_id, []),
                               row['tool_name'], row['input_sha256'], row['failure']]) for _, row in matches
                              if row.get('runtime_binding', UNKNOWN_RUNTIME) == UNKNOWN_RUNTIME]
                for legacy_id in sorted(set(legacy_ids)):
                    if c.execute('SELECT 1 FROM work_requests WHERE id=?', (legacy_id,)).fetchone():
                        ident = legacy_id
                        break
            prior = c.execute('SELECT * FROM work_requests WHERE id=?', (ident,)).fetchone()
            if finding_id:
                if prior is None or prior['kind'] != 'finding' or prior['recipient_role'] != 'maintainer':
                    raise ValueError('Select an existing original maintenance finding')
                if prior['task_id'] != task_id:
                    if not bound or not bound.get(task_id):
                        raise ValueError('Cross-task confirmation needs the affected original request')
                    for request_id in bound[task_id]:
                        if tasks._row(c, 'work_requests', request_id)['task_id'] != task_id:
                            raise ValueError('Affected request belongs to a different original task')
                    finding_task = tasks._row(c, 'work_tasks', prior['task_id'])
            if prior is None:
                requests = bound.get(task_id, [])
                fields = {'task_id': task_id, 'kind': 'finding', 'sender_actor': tasks.actor_key(actor),
                    'recipient_actor': None, 'recipient_role': 'maintainer', 'input_ref_json': tasks._json(fresh[0]),
                    'summary': doc['summary'], 'plan_revision': task['plan_revision'], 'required_scope_json': '[]',
                    'independent': 0, 'requires_review': 0, 'authors_json': '[]',
                    'parent_request_id': requests[0] if len(requests) == 1 else None, 'supersedes_request_id': None}
                tasks._immutable(c, 'work_requests', ident, fields)
                if len(fresh) == 1:
                    # The finding already binds the first exact observation.
                    return {'status': 'registered', 'request_id': ident, 'task_id': task_id,
                            'finding_task_id': task_id, 'source_ref': ref}
        kind = 'maintenance-observation-triaged' if dismiss else 'maintenance-observation'
        payload = {'result_ref': fresh[0], 'summary': dismiss or doc['summary'], 'affected': affected_request(doc, task_id)}
        if len(fresh) > 1:
            payload['occurrence_refs'] = fresh[1:]
        if ident:
            payload['request_id'] = ident
            if problem := common_problem(doc):
                payload['problem_id'] = 'tool-problem:' + digest(problem)
        tasks._event(c, finding_task, kind, actor, payload)
        return {'status': 'triaged' if dismiss else 'registered',
                'request_id': ident, 'task_id': task_id, 'finding_task_id': finding_task['id'] if ident else None,
                'source_ref': ref}


def handle(root, event):
    root = Path(root).resolve()
    if not isinstance(event, dict):
        raise ValueError('Hook input must be an original event object')
    # Older installed hooks may still send these events. They no longer
    # read, summarize, inject or cache instructions for the agent.
    if event.get('hook_event_name') != 'PostToolUse':
        return {}
    actor = actor_for(root, event)
    record_failure(root, actor, event)
    return {}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=ROOT)
    parser.add_argument('action', nargs='?', choices=('event', 'pending', 'link'), default='event')
    parser.add_argument('--receipt', type=Path)
    parser.add_argument('--task')
    parser.add_argument('--all', action='store_true', help='Inspect all retained receipts as the designated maintainer')
    parser.add_argument('--limit', type=int, default=20)
    parser.add_argument('--offset', type=int, default=0)
    parser.add_argument('--problem', help='Expand exact retained observations for one candidate common problem')
    resolution = parser.add_mutually_exclusive_group()
    resolution.add_argument('--finding', help='Confirm the same cause and link to an existing maintenance finding')
    resolution.add_argument('--dismiss', help='Triage reason when an original observation does not need a finding')
    args = parser.parse_args()
    try:
        if args.action == 'event':
            raw = sys.stdin.buffer.read(2 * 1024 * 1024 + 1)
            if len(raw) > 2 * 1024 * 1024:
                raise ValueError('Hook input exceeds the bounded 2 MiB reader')
            value = handle(args.root, json.loads(raw))
        else:
            actor = None if args.action == 'pending' and args.all else tasks.verified_actor(args.root)
            if args.action == 'pending':
                rows = pending_observations(args.root, actor, all_actors=args.all, problem_id=args.problem)
                value = triage_page(rows, limit=args.limit, offset=args.offset, problem_id=args.problem) if args.all else rows
            else:
                if args.receipt is None:
                    raise ValueError('Link requires the exact --receipt')
                value = link_observation(args.root, actor, args.receipt, task_id=args.task,
                                         finding_id=args.finding, dismiss=args.dismiss)
        print(json.dumps(value, ensure_ascii=False))
    except (ValueError, OSError, sqlite3.Error, subprocess.TimeoutExpired) as exc:
        if args.action == 'event':
            print(json.dumps({'systemMessage': 'Harness entry needs attention: ' + str(exc)}, ensure_ascii=False))
            return 0
        print(json.dumps({'error': str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
