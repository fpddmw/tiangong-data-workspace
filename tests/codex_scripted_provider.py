"""Loopback Responses fixture; no external model or credential is used."""
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from uuid import uuid4


class ScriptedProvider:
    def __init__(self, respond):
        self.requests = []
        self.errors = []
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                try:
                    raw = self.rfile.read(int(self.headers['Content-Length']))
                    if self.headers.get('Content-Encoding') == 'zstd':
                        import zstandard
                        raw = zstandard.ZstdDecompressor().decompress(raw)
                    request = json.loads(raw)
                    fixture.requests.append(request)
                    item = respond(request)
                    response = {'id': 'resp_' + uuid4().hex, 'object': 'response',
                                'status': 'completed', 'output': [item],
                                'usage': {'input_tokens': 1, 'output_tokens': 1, 'total_tokens': 2}}
                    events = [
                        {'type': 'response.created', 'response': {**response, 'status': 'in_progress', 'output': []}},
                        {'type': 'response.output_item.added', 'output_index': 0, 'item': item},
                        {'type': 'response.output_item.done', 'output_index': 0, 'item': item},
                        {'type': 'response.completed', 'response': response},
                    ]
                    body = ''.join('event: ' + e['type'] + '\ndata: ' + json.dumps(e) + '\n\n' for e in events).encode()
                    self.send_response(200)
                    self.send_header('Content-Type', 'text/event-stream')
                    self.send_header('Content-Length', str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                except Exception as exc:
                    fixture.errors.append(repr(exc))
                    self.send_error(500)

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *args):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    @property
    def url(self):
        return f'http://127.0.0.1:{self.server.server_port}/v1'

    @staticmethod
    def message(text):
        return {'id': 'msg_' + uuid4().hex, 'type': 'message', 'role': 'assistant',
                'status': 'completed', 'content': [{'type': 'output_text', 'text': text, 'annotations': []}]}

    @staticmethod
    def tool(name, arguments):
        return {'id': 'fc_' + uuid4().hex, 'call_id': 'call_' + uuid4().hex, 'type': 'function_call',
                'name': name, 'arguments': json.dumps(arguments), 'status': 'completed'}
