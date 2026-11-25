"""Small, synthetic Process, Flow and CLI evidence for review tests."""
from hashlib import sha256
import json
from pathlib import Path


PROCESS_ID = 'dcf6fb7c-bd47-460e-8295-a44459fdd6e3'
FLOW_ID = 'eaa0cfce-c98b-4420-88a1-9a54b569e777'
VERSION = '00.00.001'


def process(*, amount='0.125', identifier=PROCESS_ID, flow_version=VERSION):
    return {'processDataSet': {
        'processInformation': {
            'dataSetInformation': {
                'common:UUID': identifier,
                'name': {'baseName': [{'@xml:lang': 'en', '#text': 'Synthetic dry product'}]},
                'common:generalComment': [
                    {'@xml:lang': 'en', '#text': 'Per kg of dry product.'},
                    {'@xml:lang': 'zh', '#text': '以干产品为基准。'},
                ],
            },
            'quantitativeReference': {'@type': 'Reference flow(s)', 'referenceToReferenceFlow': '1'},
        },
        'administrativeInformation': {'publicationAndOwnership': {
            'common:dataSetVersion': VERSION,
        }},
        'exchanges': {'exchange': [
            {'@dataSetInternalID': '1', 'meanAmount': '1', 'resultingAmount': '1', 'exchangeDirection': 'Output',
             'referenceToFlowDataSet': {'@type': 'flow data set', '@refObjectId': FLOW_ID, '@version': flow_version}},
            {'@dataSetInternalID': '2', 'meanAmount': amount, 'resultingAmount': amount, 'exchangeDirection': 'Input',
             'referenceToFlowDataSet': {'@type': 'flow data set', '@refObjectId': FLOW_ID, '@version': flow_version}},
        ]},
    }}


def flow(version=VERSION, *, label='Synthetic dry input'):
    return {'flowDataSet': {
        'flowInformation': {'dataSetInformation': {'common:UUID': FLOW_ID,
            'name': {'baseName': [{'@xml:lang': 'en', '#text': label}]}}},
        'administrativeInformation': {'publicationAndOwnership': {'common:dataSetVersion': version}},
    }}


def revision_documents():
    next_version = '00.00.002'
    before = process(amount='0.125')
    after = process(amount='0.250', flow_version=next_version)
    for doc, ident in ((before, FLOW_ID), (after, PROCESS_ID)):
        doc['processDataSet']['administrativeInformation']['publicationAndOwnership']['common:permanentDataSetURI'] = (
            f'https://data.tiangong.earth/processes/{ident}_{VERSION}.xml')
    info = after['processDataSet']['processInformation']['dataSetInformation']
    info['common:generalComment'][1]['#text'] = '以新测量值描述干产品。'
    return {'rows': [before, flow()]}, {'rows': [after, flow(next_version, label='Revised synthetic dry input')]}


def save(path, body):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(body, ensure_ascii=False).encode()
    path.write_bytes(raw)
    return {'path': str(path), 'sha256': sha256(raw).hexdigest()}


def readback(archive, name, *, run_id, request_id, finished_at, body):
    target = archive / ('operation-' + name)
    subject = {'type': 'process', 'id': body['processDataSet']['processInformation']['dataSetInformation']['common:UUID'],
               'version': VERSION, 'owner': 'synthetic-owner'}
    stdout = save(target/'stdout.json', {
        'schemaVersion': 1, 'operation': 'dataset process get', 'status': 'completed', 'outcome': 'dataset_read',
        'data': {'subject': subject}, 'context': {'environment': 'synthetic-platform',
            'observation': {'source': 'remote'}},
        'completeness': {'returned': 1, 'hasMore': False, 'truncated': False},
    })
    snapshot = save(target/'output.json', body)
    save(target/'record.json', {
        'schema': 'htam.delivery-native-cli.v1', 'source': 'trusted_public_cli',
        'run_id': run_id, 'request_id': request_id, 'operation': 'dataset process get',
        'capture_complete': True, 'exit_code': 0, 'finished_at': finished_at, 'stdout_ref': stdout,
        'output_fresh': True, 'output_ref': snapshot,
    })
    return snapshot
