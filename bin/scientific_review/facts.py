"""Deterministic facts from a review's frozen files and original CLI archives."""
import contextlib
from collections import defaultdict
from hashlib import sha256
import json
from pathlib import Path

import delivery_evidence as delivery
import harness_tasks as tasks
from dataset_checks import projection as project
from harness_package import author_lineage, launch_spec
from dataset_checks.references import REFERENCE_TYPES, nested_references, object_value, reference_body
from production_review import scientific_path
from scientific_review.delta import packet_bodies

SCHEMA = 'htam.independent-review-facts.v1'
PROCESS_FIELDS = (
    '/processInformation/quantitativeReference',
    '/processInformation/technology',
    '/processInformation/dataSetInformation/common:generalComment',
    '/modellingAndValidation/LCIMethodAndAllocation',
    '/modellingAndValidation/dataSourcesTreatmentAndRepresentativeness',
)


def _author_archives(root, request, author_run_id, cutoff):
    """Read only the submitting run and its actual predecessors in this lineage."""
    with contextlib.closing(tasks._connect(root, True)) as conn:
        scope, seen = set(author_lineage(conn, request)), set()
        run_id = author_run_id
        while run_id and run_id not in seen:
            seen.add(run_id)
            row = conn.execute('SELECT * FROM work_runs WHERE id=?', (run_id,)).fetchone()
            if row is None or row['request_id'] not in scope or row['created_at'] > cutoff:
                break
            yield tasks._decode(row)
            intent = conn.execute("SELECT payload_json FROM work_run_events WHERE run_id=? AND kind='intent' "
                                  'ORDER BY id LIMIT 1', (run_id,)).fetchone()
            run_id = json.loads(intent[0]).get('previous_run_id') if intent else None


def _operation_records(root, request, author_run_id, cutoff):
    limit = delivery._time(cutoff)
    for run in _author_archives(root, request, author_run_id, cutoff):
        if not (home := launch_spec(root, run).get('evidence_home')):
            continue
        for path in sorted((Path(home) / 'runs' / run['id'] / 'delivery').glob('operation-*/record.json')):
            try:
                path = scientific_path(root, path)
                raw = path.read_bytes()
                row = object_value(json.loads(raw))
                op = row.get('operation', '')
                finished = delivery._time(row.get('finished_at'))
                if (op not in ('dataset process get', 'dataset process create', 'dataset process save', 'dataset flow get',
                               'dataset flowproperty get', 'dataset unitgroup get')
                        or not limit or not finished or finished > limit
                        or row.get('source') != 'trusted_public_cli' or row.get('schema') != delivery.CLI_SCHEMA
                        or row.get('run_id') != run['id'] or row.get('request_id') != run['request_id']
                        or row.get('capture_complete') is not True or row.get('exit_code') != 0
                        or '--dry-run' in row.get('argv', [])):
                    continue
                yield finished, row, {'path': str(path), 'sha256': sha256(raw).hexdigest()}
            except (OSError, ValueError, TypeError):
                continue


def _readbacks(root, request, packet, cutoff, identities):
    observed = defaultdict(dict)
    for finished, record, record_ref in _operation_records(root, request, packet.get('author_run_id'), cutoff):
        reply_ref = record.get('stdout_ref')
        reply = delivery._json(root, reply_ref)
        if not delivery._read_success(reply) or reply.get('schemaVersion') != 1 or reply.get('operation') != record['operation']:
            continue
        data, context = object_value(reply.get('data')), object_value(reply.get('context'))
        subject = object_value(data.get('subject'))
        identity = delivery._subject(subject)
        environment = context.get('environment')
        if not identity or not isinstance(environment, str) or not environment:
            continue
        if identity not in identities and identity[0] not in ('flowproperty', 'unitgroup'):
            continue
        _, kind, action = record['operation'].split()
        if kind != identity[0]:
            continue
        if action == 'get':
            page = object_value(reply.get('completeness'))
            if (reply.get('outcome') != 'dataset_read' or project.at(context, 'observation', 'source') != 'remote'
                    or page.get('returned') != 1 or page.get('hasMore') is not False or page.get('truncated') is not False):
                continue
            owner = subject.get('owner')
            body, body_ref = data.get('content'), reply_ref
            if body is None and reply.get('status') == 'completed' and record.get('output_fresh') is True:
                body_ref = record.get('output_ref')
                body = delivery._json(root, body_ref)
        else:
            readback = object_value(data.get('readback'))
            if (reply.get('outcome') not in ('dataset_written', 'dataset_created', 'dataset_saved')
                    or readback.get('contentMatchesIntent') is not True or delivery._subject(readback.get('subject')) != identity):
                continue
            owner = project.at(context, 'actor', 'id')
            if not isinstance(owner, str) or not owner:
                continue
            body_ref = record.get('candidate_ref')
            body = delivery._json(root, body_ref)
        if not isinstance(body, dict) or reference_body(delivery.canonical_body(body))[0] != identity:
            continue
        # The original public reader may have no authenticated actor. Bind
        # its observation to the dataset owner reported by the platform.
        scope = (environment, owner if isinstance(owner, str) and owner else None)
        fact = {'operation': record['operation'], 'finished_at': record['finished_at'],
                'record_ref': record_ref, 'reply_ref': reply_ref, 'body_ref': body_ref}
        before = observed[identity].get(scope)
        if before is None or finished > before[0]:
            observed[identity][scope] = (finished, body, fact)
    return {identity: [(body, fact) for _, body, fact in choices.values()]
            for identity, choices in observed.items()}


def _body_comparison(candidate, readbacks):
    if not readbacks:
        return {'status': 'no_complete_readback_in_original_archives'}
    if len(readbacks) != 1:
        return {'status': 'multiple_readback_scopes', 'observations': [fact for _, fact in readbacks]}
    body, fact = readbacks[0]
    candidate = {k: v for k, v in delivery.canonical_body(candidate).items() if k != 'semantic_context'}
    body = {k: v for k, v in delivery.canonical_body(body).items() if k != 'semantic_context'}
    differences = delivery.changed_paths(candidate, body)
    same = delivery.same_body(candidate, body)
    return {**fact, 'status': 'complete_match' if same else 'complete_differs',
            'changed_pointers': differences}


def _process(identity, frozen, reference_bodies, readbacks):
    body = frozen['body']['processDataSet']
    rows, members = project.exchange_members(body)
    exchanges, flow_bindings, sources = [], defaultdict(list), defaultdict(list)
    exchange_pointer = '/processDataSet/exchanges/exchange'
    repeated = isinstance(project.at(body, 'exchanges', 'exchange'), list)
    for index, row in enumerate(rows):
        exchange = object_value(row)
        flow = exchange.get('referenceToFlowDataSet')
        chain, status = project.unit_chain(flow, reference_bodies, reference_bodies)
        identifier = exchange.get('@dataSetInternalID')
        pointer = f'{exchange_pointer}/{index}' if repeated else exchange_pointer
        exchanges.append({'id': identifier, 'pointer': pointer, 'direction': exchange.get('exchangeDirection'),
                          'mean_amount': exchange.get('meanAmount'), 'resulting_amount': exchange.get('resultingAmount'),
                          'declared_unit': exchange.get('unitName'), 'flow_ref': flow,
                          'unit_chain': chain, 'unit_status': status})
        if handle := project.at(chain, 'flow', 'handle') or project.at(chain, 'flow', 'requested'):
            flow_bindings[handle].append(identifier)
    for pointer, ref in nested_references(frozen['body']):
        if REFERENCE_TYPES.get(str(ref.get('@type', '')).casefold()) == 'source':
            ident, version = ref.get('@refObjectId'), ref.get('@version')
            if isinstance(ident, str) and (version is None or isinstance(version, str)):
                sources[(ident, version)].append(pointer)
    return {'identity': identity, 'frozen_ref': frozen['ref'], 'readback': _body_comparison(frozen['body'], readbacks),
            'basis': project.declared_process_basis(body, identity, exchanges=exchanges),
            'annual_production': project.at(body, 'modellingAndValidation', 'dataSourcesTreatmentAndRepresentativeness',
                                            'annualSupplyOrProductionVolume'),
            'source_references': [{'id': ident, 'version': version, 'pointers': pointers}
                                  for (ident, version), pointers in sources.items()],
            'field_pointers': ['/processDataSet'+p for p in PROCESS_FIELDS if project.pointer_value(body, p) is not None],
            'exchange_ids_unique': members is not None, 'exchanges': exchanges,
            'shared_flow_references': {handle: ids for handle, ids in flow_bindings.items() if len(ids) > 1}}


def collect(root, request, packet, cutoff):
    frozen, unavailable, ambiguous = packet_bodies(root, packet)
    processes = {key: value for key, value in frozen.items() if key[0] == 'process'}
    if not processes:
        return None
    flows = {key for item in processes.values() for _, ref in nested_references(item['body'])
             if (key := delivery._subject({'type': 'flow', 'id': ref.get('@refObjectId'),
                                          'version': ref.get('@version')}))
             and REFERENCE_TYPES.get(str(ref.get('@type', '')).casefold()) == 'flow'}
    readbacks = _readbacks(root, request, packet, cutoff, processes.keys() | flows)
    references = {'/'.join(key): value['body'] for key, value in frozen.items()}
    verified_flows = {}
    for key in sorted(flows):
        handle = '/'.join(key)
        if handle not in references and len(readbacks.get(key, [])) == 1 and key not in ambiguous:
            body, fact = readbacks[key][0]
            references[handle], verified_flows[handle] = delivery.canonical_body(body), fact
    # Bind only property and unit bodies actually reached from the reviewed exchanges.
    verified_units = {}
    for component in ('property', 'unit_group'):
        for flow in flows:
            flow_ref = {'@type':'flow data set', '@refObjectId':flow[1], '@version':flow[2]}
            chain, _ = project.unit_chain(flow_ref, references, references)
            missing = project.at(chain, component, 'requested')
            if not missing:
                continue
            key = tuple(missing.split('/'))
            choices = readbacks.get(key, [])
            if len(choices) == 1 and key not in ambiguous:
                body, fact = choices[0]
                references[missing] = delivery.canonical_body(body)
                verified_units[missing] = fact
    return {'schema': SCHEMA, 'author_run_id': packet['author_run_id'], 'as_of': cutoff,
            'processes': [_process(key, value, references, readbacks.get(key, [])) for key, value in processes.items()],
            'frozen_datasets': {'/'.join(key): value['ref'] for key, value in frozen.items() if key not in processes},
            'flow_descriptions': project.flow_descriptions(references, ('/'.join(key) for key in flows)),
            'verified_flow_reads': verified_flows, 'verified_unit_reads': verified_units, 'ambiguous_identities': ambiguous,
            'unreadable_artifacts': unavailable, 'original_artifacts': packet['artifact_refs']}
