#!/usr/bin/env python3
"""Stdio MCP transport for the existing ControlledTools dispatch.

The private, per-invocation socket belongs to the original runtime. This
module owns no review jobs, account credentials or business records.
"""
import json
import os
from pathlib import Path
import socket
import socketserver
import sys
import threading


class Bridge:
    def __init__(self, path, controlled, *, guard=lambda: None, cancelled=lambda: False):
        self.path = Path(path)
        self.controlled, self.guard, self.cancelled = controlled, guard, cancelled
        bridge = self
        class Handler(socketserver.StreamRequestHandler):
            def handle(self):
                try:
                    message = json.loads(self.rfile.readline())
                    bridge.guard()
                    if message['method'] == 'tools/list':
                        result = {'tools': [{key: value for key, value in tool.items() if key != 'type'}
                                            for tool in controlled.declarations()]}
                    elif message['method'] == 'tools/call':
                        args = message.get('params', {})
                        value = controlled.dispatch(args['name'], json.dumps(args.get('arguments') or {}),
                            guard=bridge.guard, cancelled=bridge.cancelled)
                        result = {'content': [{'type': 'text', 'text': json.dumps(value, ensure_ascii=False)}],
                                  'isError': not value['accepted']}
                    else:
                        raise ValueError('Unqualified collaboration transport request')
                    response = {'result': result}
                except Exception as exc:
                    from safe_diagnostics import safe_text
                    response = {'error': {'code': -32603, 'message': safe_text(exc)}}
                self.wfile.write((json.dumps(response, ensure_ascii=False) + '\n').encode())
        class Server(socketserver.ThreadingUnixStreamServer):
            daemon_threads = True
        self.server = Server(str(path), Handler)
        os.chmod(path, 0o600)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={'poll_interval': .1}, daemon=True)
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=1)
        self.path.unlink(missing_ok=True)


def main(path):
    for line in sys.stdin:
        message = json.loads(line)
        if 'id' not in message:
            continue
        method = message.get('method')
        if method == 'initialize':
            response = {'result': {'protocolVersion': message['params']['protocolVersion'],
                'capabilities': {'tools': {}}, 'serverInfo': {'name': 'htam-collaboration', 'version': '1'}}}
        elif method == 'ping':
            response = {'result': {}}
        elif method in ('tools/list', 'tools/call'):
            try:
                with socket.socket(socket.AF_UNIX) as connection:
                    connection.connect(path)
                    connection.sendall((json.dumps(message) + '\n').encode())
                    with connection.makefile('rb') as reader:
                        response = json.loads(reader.readline())
            except (OSError, ValueError):
                response = {'error': {'code': -32603, 'message': 'Original invocation bridge unavailable; retain original review handle'}}
        else:
            response = {'error': {'code': -32601, 'message': 'Unsupported MCP transport method'}}
        print(json.dumps({'jsonrpc': '2.0', 'id': message['id'], **response}, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main(sys.argv[1])
