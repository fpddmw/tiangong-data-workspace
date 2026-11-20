"""Admission for exact managed dataset mutations at the trusted CLI launcher."""
from contextlib import closing
import json
from urllib.parse import urlsplit

import harness_decisions as decisions
import harness_tasks as tasks
from harness_finite_work import _package_id, request_configs
from lca_gate_references import UUID, VERSION, reference_body


def public_argv(argv):
    # Match the installed CLI's outer argument normalization.
    if argv[:1] == ['--'] and argv[1:2] != ['--']:
        return argv[1:]
    if argv[:1] == ['--json'] and len(argv) > 1:
        tail = argv[1:]
        split = tail.index('--') if '--' in tail else len(tail)
        return [*tail[:split], '--json', *tail[split:]]
    return argv


def exact_action(argv):
    if any(flag in argv for flag in ('--help', '-h', '--dry-run')):
        return None
    if len(argv) >= 3 and argv[0] == 'dataset' and argv[2] in ('create', 'save', 'delete'):
        return argv[1], argv[2]
    return None


def admit(root, run, action, raw, env):
    """Resolve the owner's live decision using the bytes passed to the CLI."""
    kind, verb = action
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError('Managed mutation requires an exact native request object.')
    ident, version = value.get('id'), value.get('version')
    if not isinstance(ident, str) or not UUID.fullmatch(ident) or not isinstance(version, str) or not VERSION.fullmatch(version):
        raise ValueError('Managed mutation requires an exact native UUID and version.')
    content = value.get('expectedContent') if verb == 'delete' else value.get('content')
    key, _ = reference_body(content)
    canonical_kind = 'lifecyclemodel' if kind == 'model' else kind
    if key != (canonical_kind, ident, version):
        raise ValueError('Managed mutation target conflicts with the candidate identity.')

    account = env.get('TIANGONG_LCA_EXPECTED_USER_ID')
    project = env.get('TIANGONG_LCA_EXPECTED_PROJECT_REF')
    environment = value.get('expectedEnvironment')
    origin = urlsplit(environment) if isinstance(environment, str) else None
    configured_url = env.get('TIANGONG_LCA_API_BASE_URL')
    configured = urlsplit(configured_url) if configured_url else None
    if (not account or not project or value.get('expectedActor') != account
            or origin is None or origin.scheme not in ('https', 'http') or not origin.hostname
            or (configured and (origin.scheme, origin.netloc) != (configured.scheme, configured.netloc))):
        raise ValueError('Managed mutation request conflicts with the configured actor or project.')

    with closing(tasks._connect(root, True, timeout=1)) as c:
        tasks.ensure_schema(c)
        request_id = tasks._row(c, 'work_runs', run['id'])['request_id']
        if request_id != run['request_id']:
            raise ValueError('Managed mutation run does not match its original request.')
        request = tasks._decode(tasks._row(c, 'work_requests', request_id))
        task = request['task_id']
        package = _package_id(request, request_configs(c, task))
        scope = {'task': task, 'package': package, 'object': f'{kind}/{ident.lower()}/{version}',
                 'action': verb, 'project': project, 'account': account}
        active = decisions.resolve(root, scope, subject=decisions.DATASET_WRITE, connection=c)['subjects']

    result = {'scope': scope, 'decisions': [row['id'] for item in active for row in item['decisions']]}
    if active:
        rule, = active
        if rule['status'] != 'applicable':
            return {**result, 'status': rule['status']}
        if rule['decisions'][0]['statement'] == 'stop':
            return {**result, 'status': 'stopped'}
    # The native CLI reads its original journal by caller requestId.
    # An unresolved object with a no-replay direction stays covered by its
    # current named stop, including attempts that substitute a requestId.
    return {**result, 'status': 'admitted'}
