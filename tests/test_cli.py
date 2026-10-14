import json
import os
from pathlib import Path
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import pytest


def run(*args, cwd=None):
    env = os.environ.copy()
    env['PATH'] = str(Path.home() / '.local/bin') + os.pathsep + env['PATH']
    return subprocess.run([sys.executable, '-m', 'mocker', *map(str, args)], cwd=cwd,
                          env=env, capture_output=True, text=True, timeout=40)


def test_cli_module_scope_replay(tmp_path):
    (tmp_path / 'app').mkdir()
    (tmp_path / 'app/__init__.py').write_text('')
    (tmp_path / 'app/users.py').write_text('def get_name(x):\n return "Ryan"\n')
    (tmp_path / 'app/__main__.py').write_text('from .users import get_name\nprint(get_name(42))')
    traces = tmp_path / 'functions.txt'
    traces.write_text('# comment\napp.users:get_name\n')
    cassette = tmp_path / 'demo.json'
    result = run('forward', '--runner', 'python', '--scope', 'app', '--trace-file', traces,
                 '--cassette', cassette, '--', 'python', '-m', 'app', cwd=tmp_path)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == 'Ryan'
    (tmp_path / 'app/users.py').write_text('def get_name(x):\n raise AssertionError("executed")')
    result = run('reverse', '--runner', 'python', '--cassette', cassette, '--', 'python', '-m', 'app', cwd=tmp_path)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == 'Ryan'


def test_strict_offline_never_launches(tmp_path):
    marker = tmp_path / 'ran'
    script = tmp_path / 'script.py'
    script.write_text(f'from pathlib import Path\nPath({str(marker)!r}).touch()')
    result = run('reverse', '--offline', 'strict', '--cassette', tmp_path / 'absent.json', '--', sys.executable, script)
    assert result.returncode == 2
    assert not marker.exists()
    assert 'not launched' in result.stderr


def test_http_record_replay_and_miss(tmp_path):
    pytest.importorskip('mitmproxy')
    class Handler(BaseHTTPRequestHandler):
        calls = 0
        def do_GET(self):
            type(self).calls += 1
            self.send_response(200)
            self.send_header('X-Duplicate', 'one')
            self.send_header('X-Duplicate', 'two')
            self.end_headers()
            self.wfile.write(b'\x00binary\xff')
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    client = tmp_path / 'client.py'
    url = f'http://127.0.0.1:{server.server_port}/data'
    client.write_text(f'import urllib.request\nprint(urllib.request.urlopen({url!r}).read().hex())')
    cassette = tmp_path / 'http.json'
    try:
        result = run('forward', '--cassette', cassette, '--', sys.executable, client)
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == '0062696e617279ff'
        assert Handler.calls == 1
        empty = tmp_path / 'empty.json'
        empty.write_text(json.dumps({'version': 1, 'complete': True, 'config': {}, 'interactions': []}))
        result = run('reverse', '--cassette', empty, '--', sys.executable, client)
        assert result.returncode != 0
        assert Handler.calls == 1
        assert '502' in result.stderr
    finally:
        server.shutdown()
        server.server_close()
    result = run('reverse', '--cassette', cassette, '--', sys.executable, client)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == '0062696e617279ff'
    event = json.loads(cassette.read_text())['interactions'][0]
    assert [v for k, v in event['outcome']['response']['headers'] if k.lower() == 'x-duplicate'] == ['one', 'two']


def test_https_mock_uses_trusted_local_ca_without_upstream(tmp_path):
    pytest.importorskip('mitmproxy')
    config = tmp_path / 'config.json'
    cassette = tmp_path / 'empty.json'
    cassette.write_text(json.dumps({'version': 1, 'complete': True, 'interactions': []}))
    config.write_text(json.dumps({'rules': [{'select': {'kind': 'http', 'host': 'offline.invalid'},
        'action': {'type': 'mock', 'outcome': {'response': {'status': 200,
        'headers': [['Content-Type', 'application/json']], 'body': {'json': {'name': 'Ryan'}}}}}}]}))
    client = tmp_path / 'https.py'
    client.write_text('import ssl, pathlib, urllib.request\n'
        'context = ssl.create_default_context(cafile=str(pathlib.Path.home() / ".mitmproxy/mitmproxy-ca-cert.pem"))\n'
        'print(urllib.request.urlopen("https://offline.invalid/me", context=context).read().decode())')
    result = run('reverse', '--config', config, '--cassette', cassette, '--', sys.executable, client)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {'name': 'Ryan'}


def test_cli_sample_seed_and_forward_rejection(tmp_path):
    script = tmp_path / 'program.py'
    script.write_text('values = [10, 20, 30]\ndef f():\n return values.pop(0)\nfor _ in range(3):\n print(f())')
    cassette = tmp_path / 'sample.json'
    result = run('forward', '--runner', 'python', '--trace', 'f', '--cassette', cassette, '--', sys.executable, script)
    assert result.returncode == 0, result.stderr
    script.write_text('def f():\n raise AssertionError\nfor _ in range(20):\n print(f())')
    options = ('reverse', '--runner', 'python', '--sample', '--seed', '42', '--cassette', cassette, '--', sys.executable, script)
    first, second = run(*options), run(*options)
    assert first.returncode == second.returncode == 0
    assert first.stdout == second.stdout
    assert set(map(int, first.stdout.splitlines())) == {10, 20, 30}
    result = run('forward', '--sample', '--cassette', tmp_path / 'new.json', '--', sys.executable, script)
    assert result.returncode == 2
    assert not (tmp_path / 'new.json').exists()


def test_http_proxy_sampled_responses_without_upstream(tmp_path):
    pytest.importorskip('mitmproxy')
    class Handler(BaseHTTPRequestHandler):
        calls = 0
        def do_GET(self):
            type(self).calls += 1
            self.send_response(200)
            self.end_headers()
            self.wfile.write(str(type(self).calls).encode())
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f'http://127.0.0.1:{server.server_port}/status'
    client = tmp_path / 'client.py'
    def source(n):
        return f'import urllib.request\nfor _ in range({n}):\n print(urllib.request.urlopen({url!r}).read().decode())'
    client.write_text(source(2))
    cassette = tmp_path / 'http-sample.json'
    try:
        result = run('forward', '--cassette', cassette, '--', sys.executable, client)
        assert result.returncode == 0, result.stderr
        assert Handler.calls == 2
    finally:
        server.shutdown()
        server.server_close()
    client.write_text(source(20))
    result = run('reverse', '--sample', '--seed', '42', '--cassette', cassette, '--', sys.executable, client)
    assert result.returncode == 0, result.stderr
    assert len(result.stdout.splitlines()) == 20
    assert set(result.stdout.splitlines()) == {'1', '2'}
