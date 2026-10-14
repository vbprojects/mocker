"""Optional mitmproxy addon. Protocol bodies are represented as base64 bytes."""
import base64
import json
import os
from urllib.parse import urlsplit
from .config import Config
from .engine import Engine
from .storage import Cassette


def request_event(request):
    parsed = urlsplit(request.pretty_url)
    return {'kind': 'http', 'target': f'{request.method} {parsed.scheme}://{parsed.netloc}{parsed.path}',
            'method': request.method, 'host': request.host, 'path': parsed.path,
            'input': {'method': request.method, 'scheme': request.scheme, 'host': request.host,
                      'port': request.port, 'path': request.path,
                      'body_base64': base64.b64encode(request.raw_content or b'').decode(),
                      # Authorization identity remains in matching inputs; cassettes must be treated as secrets.
                      'headers': [list(x) for x in request.headers.items(multi=True)]}}


def response_spec(response):
    return {'status': response.status_code, 'headers': [list(x) for x in response.headers.items(multi=True)],
            'body': {'base64': base64.b64encode(response.raw_content or b'').decode()}}


def make_response(spec):
    from mitmproxy import http
    body = spec.get('body', {})
    if 'json' in body:
        content = json.dumps(body['json']).encode()
    elif 'base64' in body:
        content = base64.b64decode(body['base64'], validate=True)
    else:
        content = body.get('text', '').encode()
    headers = [(str(k).encode('latin-1'), str(v).encode('latin-1')) for k, v in spec.get('headers', [])]
    return http.Response.make(spec.get('status', 200), content, headers)


class Addon:
    def __init__(self, engine):
        self.engine = engine
        self.pending = {}

    def request(self, flow):
        event = request_event(flow.request)
        if not self.engine.config.selected(event, self.engine.mode):
            if self.engine.mode == 'reverse':
                flow.response = make_response({'status': 502, 'body': {'text': 'mocker: unselected request blocked during replay'}})
            return
        try:
            record, action, outcome = self.engine.prepare(event)
            if outcome is not None:
                if 'response' not in outcome:
                    raise ValueError('HTTP outcome must contain response')
                flow.response = make_response(outcome['response'])
            else:
                self.pending[flow.id] = record, action
        except Exception as exc:
            flow.response = make_response({'status': 502, 'body': {'text': f'mocker: {exc}'}})

    def response(self, flow):
        if flow.id in self.pending:
            record, action = self.pending.pop(flow.id)
            self.engine.finish(record, action, {'response': response_spec(flow.response)})

    def error(self, flow):
        if flow.id in self.pending:
            record, action = self.pending.pop(flow.id)
            if action['type'] == 'record':
                self.engine.destination.append({**record, 'outcome': {}, 'replayability': 'unsupported',
                                                'error': 'HTTP transport failure: ' + str(flow.error)})

    def done(self):
        if self.engine.destination:
            self.engine.destination.close(complete=not self.pending)


def from_environment():
    data = json.loads(os.environ['MOCKER_HTTP_CONFIG'])
    config = Config(data['config'])
    source = Cassette(data['source']) if data.get('source') else None
    destination = Cassette(data['destination'], write=True, config=config.data) if data.get('destination') else None
    return Addon(Engine(config, data['mode'], source, destination))
