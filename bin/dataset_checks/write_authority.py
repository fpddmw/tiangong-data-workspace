"""Admission for exact managed dataset mutations at the trusted CLI launcher."""
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
from urllib.parse import urlsplit

from task_ledger import decisions
import harness_tasks as tasks
from delivery_evidence import operation_effect, public_argv
from task_ledger.finite_work import _package_id, request_configs
from dataset_checks.references import UUID, VERSION, reference_body
from dataset_checks.native_journal import pending_writes


FLOW_TYPES = ('Elementary flow', 'Product flow', 'Waste flow')


def exact_action(argv):
    argv = public_argv(argv)
    if (operation_effect(argv)=='possible_write' and len(argv)>=3
            and argv[0]=='dataset' and argv[2] in ('create','save','delete')):
        return argv[1], argv[2]
    if argv[:3]==['dataset','model','result-group'] and operation_effect(argv)=='possible_write':
        return 'model','result-group'
    return None


def _flow_type(content):
    try:
        kind = content['flowDataSet']['modellingAndValidation']['LCIMethod']['typeOfDataSet']
    except (KeyError, TypeError):
        kind = None
    if kind not in FLOW_TYPES:
        raise ValueError('The exact Flow content must establish its elementary, product or waste category.')
    return kind


def _identity(kind, ident, version):
    if not isinstance(ident, str) or not UUID.fullmatch(ident) or not isinstance(version, str) or not VERSION.fullmatch(version):
        raise ValueError('Managed mutation requires an exact native UUID and version.')
    return f'{kind}/{ident.lower()}/{version}'


def _binding(value, env):
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
    return account, project, environment


def literal_request(action, raw, env):
    """Bind the exact target and both the compare-and-swap and proposed bodies."""
    kind, verb = action
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError('Managed mutation requires an exact native request object.')
    if verb=='result-group':
        if value.get('action') in ('inspect','recover'):
            return None
        if value.get('action')!='save' or 'baseline' not in value:
            raise ValueError('Use the documented Model result-group save, inspect or recover request.')
        account,project,environment=_binding(value,env)
        target=_identity(kind,value.get('modelId'),value.get('version'))
        return {'object':target,'action':verb,'account':account,'project':project,'environment':environment,
                'categories':[],'request':value,'requires_grant':False}
    ident, version = value.get('id'), value.get('version')
    target = _identity(kind, ident, version)
    content = value.get('expectedContent') if verb == 'delete' else value.get('content')
    canonical_kind = 'lifecyclemodel' if kind == 'model' else kind
    bodies = [content, value.get('expectedContent')] if verb == 'save' else [content]
    for body in bodies:
        key, _ = reference_body(body)
        if key != (canonical_kind, ident, version):
            raise ValueError('Managed mutation target conflicts with the candidate or expected content identity.')
    categories = sorted({_flow_type(body) for body in bodies}) if kind == 'flow' else []
    account, project, environment = _binding(value, env)
    return {'object':target, 'action':verb, 'account':account, 'project':project,
            'environment':environment, 'categories':categories, 'request':value,
            'requires_grant': verb == 'delete' or 'Elementary flow' in categories}


def freeze_group_input(root, home, raw, original_path):
    """Preserve the installed adapter's relative-file semantics for frozen stdin."""
    value=json.loads(raw)
    if not isinstance(value,dict) or value.get('action')!='save':
        return raw, []
    from production_review import scientific_path, _save_fixed
    def resolved(value):
        path=Path(value)
        if not path.is_absolute():
            if original_path is None:
                raise ValueError('A Model result-group request on stdin uses absolute file references.')
            path=original_path.parent/path
        return str(scientific_path(root,path))

    members=[]
    if value.get('baseline') is not None:
        source=Path(resolved(value['baseline']))
        source_bytes=source.read_bytes()
        before=json.loads(source_bytes)
        if (before.get('modelId')!=value.get('modelId') or before.get('version')!=value.get('version')
                or before.get('expectedActor')!=value.get('expectedActor')
                or before.get('expectedEnvironment')!=value.get('expectedEnvironment')):
            raise ValueError('The Model result-group baseline does not bind this exact Model, account and environment.')
        members=[_identity('process',item.get('id'),item.get('version')) for item in before['snapshot']['members']]
        value['baseline']=_save_fixed(root,home/'model-group-baseline.json',source_bytes)['path']
    generation=value.get('generation',{})
    model=generation.get('source_model',{})
    sources=generation.get('process_sources',{})
    if name:=model.get('json_ordered_path'):
        model['json_ordered_path']=resolved(name)
    if name:=sources.get('process_catalog_path'):
        sources['process_catalog_path']=resolved(name)
    for field in ('process_json_files','process_json_dirs','run_dirs'):
        if sources.get(field):
            sources[field]=[resolved(name) for name in sources[field]]
    return json.dumps(value,ensure_ascii=False).encode(), members


def target_lock(root, request):
    from env_run import cli_state_home
    from execution_lease import FileLock
    return FileLock(root, ['dataset-mutation',request['environment'],request['object']],
                    directory=cli_state_home(root)/'tiangong-lca-cli'/'target-locks')


def admit(root, run, mutation, env, *, pending=None):
    """Resolve live controls and authorizations for the exact typed request."""
    account, project = mutation['account'], mutation['project']

    with closing(tasks._connect(root, True, timeout=1)) as c:
        tasks.ensure_schema(c)
        row = tasks._row(c, 'work_runs', run['id'])
        request_id = row['request_id']
        if request_id != run['request_id']:
            raise ValueError('Managed mutation run does not match its original request.')
        request = tasks._decode(tasks._row(c, 'work_requests', request_id))
        task = request['task_id']
        package = _package_id(request, request_configs(c, task))
        scope = {'task': task, 'package': package, 'object':mutation['object'],
                 'action':mutation['action'], 'project':project, 'account':account}
        result = {'scope':scope, 'decisions':[], 'flow_categories':mutation['categories']}
        from harness_scope import effective_contract, endpoint
        if request['independent'] or endpoint(effective_contract(c,request)) in ('evidence','candidate'):
            return {**result,'status':'read_only_assignment'}
        from delivery_evidence import _json
        brief = _json(root,request['input_ref'])
        if isinstance(brief,dict) and brief.get('data_support') is False:
            return {**result,'status':'read_only_assignment'}
        original_task = tasks._row(c, 'work_tasks', task)
        controls = tasks._delegation_decisions(c, task)
        if (original_task['state']!='active' or row['stop_requested'] or row['outcome'] in ('stopped','failed','unknown','succeeded')
                or controls.get(request_id,{}).get('action')=='end'):
            return {**result, 'status':'stopped'}
        expanded=[scope]
        if mutation['action']=='result-group':
            expanded.append({**scope, 'action':'save' if mutation['request'].get('baseline') is not None else 'create'})
            expanded.extend({**scope,'action':action,'object':member}
                            for member in mutation.get('members',[]) for action in ('save','delete'))
        controls = [d for item in expanded
                    for d in decisions.resolve(root,item,subject=decisions.DATASET_WRITE,connection=c)['subjects']]
        grants = decisions.resolve(root, scope, subject=decisions.DATASET_WRITE_GRANT, connection=c)['subjects'] if mutation['requires_grant'] else []
    for rule in controls:
        result['decisions'].extend(d['id'] for d in rule['decisions'])
        if rule['status'] != 'applicable':
            return {**result, 'status': rule['status']}
        if any(d['statement']=='stop' for d in rule['decisions']):
            return {**result, 'status': 'stopped'}
    if mutation['requires_grant']:
        if not grants:
            return {**result,'status':'authorization_required'}
        grant, = grants
        result['decisions'].extend(d['id'] for d in grant['decisions'])
        if grant['status']!='applicable' or not all(d['statement']=='allow' and d['scope']==scope for d in grant['decisions']):
            return {**result,'status':'authorization_unverified'}
    if pending is None:
        pending = pending_writes(env,mutation)
    if pending:
        return {**result,'status':'original_effect_unknown','attempts':pending}
    return {**result, 'status': 'admitted'}
