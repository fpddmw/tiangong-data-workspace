"""Offline ACP peer exercising the public author and review launch paths."""
import json
import os
from pathlib import Path
import socket
import sys

work = Path.cwd() / 'work' / 'original'
reviewer = os.environ['HTAM_DEVIN_REQUEST'] != 'request'


def call_tool(name, **arguments):
    with socket.socket(socket.AF_UNIX) as client:
        client.connect(bridge)
        message = {'method': 'tools/call', 'params': {'name': name, 'arguments': arguments}}
        client.sendall((json.dumps(message) + '\n').encode())
        packet = json.loads(client.makefile().readline())
    tool = json.loads(packet['result']['content'][0]['text'])
    assert tool['accepted'], tool
    return tool['diagnostic']


for line in sys.stdin:
    request = json.loads(line)
    method, args = request.get('method'), request.get('params', {})
    if method == 'initialize':
        result = {'protocolVersion': 1, 'agentCapabilities': {'loadSession': True}}
    elif method in ('session/new', 'session/load'):
        resumed = method == 'session/load'
        sid = args.get('sessionId') or ('fixture-reviewer' if reviewer else 'fixture-author')
        bridge = args['mcpServers'][0]['args'][-1]
        result = {'sessionId': sid, 'configOptions': [{'id': 'model', 'currentValue': 'swe-2-max',
                    'options': [{'value': 'swe-2-max'}]}]}
    elif method == 'session/prompt':
        text = 'Original author completed.'
        if reviewer:
            text = 'Checked the frozen input. Original opinion for the author.'
        elif not resumed:
            handle = call_tool('request_review', question='Check the frozen fixture input.',
                               artifacts=[str(work / 'input.json')])['review_id']
            opinion = call_tool('review_result', review_id=handle, wait_seconds=60)
            assert opinion['state'] == 'received', opinion
            (work / 'received').write_text(handle)
        print(json.dumps({'jsonrpc': '2.0', 'method': 'session/update', 'params': {'sessionId': sid,
              'update': {'sessionUpdate': 'agent_message_chunk', 'content': {'type': 'text', 'text': text}}}}), flush=True)
        result = {'stopReason': 'end_turn'}
    elif method in ('session/close', 'session/set_mode'):
        result = {}
    else:
        continue
    print(json.dumps({'jsonrpc': '2.0', 'id': request['id'], 'result': result}), flush=True)
