"""CLI and runners. Strict offline fails closed until an enforcement backend exists."""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import time
from .config import Config
from .engine import Engine, fingerprint
from .errors import MockerError
from .instrument import Finder, Instrumentor
from .storage import Cassette


def trace_file(path):
    text = Path(path).read_text()
    if text.lstrip().startswith('['):
        return json.loads(text)
    return [x.strip() for x in text.splitlines() if x.strip() and not x.lstrip().startswith('#')]


def parser():
    root = argparse.ArgumentParser(
        prog='mocker',
        description='Record Python function outcomes or HTTP requests, then replay or sample them.',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""Quick start (run from the repository root):
  # Record selected Python functions, then replay their recorded outcomes:
  mocker forward --runner python --cassette cassettes/demo.json --trace 'demo:get_name_*' -- python examples/demo.py
  mocker reverse --runner python --cassette cassettes/demo.json -- python examples/demo.py

  # Use a saved JSON config or open the interactive editor:
  mocker validate --config examples/mocker.json
  mocker forward --runner python --config examples/mocker.json -- python examples/demo.py
  mocker tui --config examples/mocker.json

  # Sample recorded outcomes with replacement (reverse only):
  mocker reverse --runner python --cassette cassettes/demo.json --sample --seed 42 -- python examples/demo.py

  # Record/replay HTTP from a proxy-aware executable (network extra required):
  mocker forward --cassette cassettes/http.json -- curl http://example.com
  mocker reverse --cassette cassettes/http.json -- curl http://example.com

Configuration and execution:
  Put Mocker options before -- and the target command after it.
  --runner python uses this interpreter; use --scope myapp for imported code.
  The default exec runner uses an HTTP proxy; HTTPS clients must trust its CA.
  Recording requires a fresh cassette path; existing files are not overwritten.
  Reverse restores config from the cassette unless --config is supplied.
  Replay does not reproduce side effects or enforce OS network isolation.
  Install optional features from a checkout: python -m pip install -e '.[tui,network]'

More help: mocker <command> --help
Config and agent guide: AGENTS.md in https://github.com/vbprojects/mocker
""")
    sub = root.add_subparsers(dest='command', required=True)
    p = sub.add_parser('tui', help='Edit configurations and run programs interactively')
    p.add_argument('--config', help='Configuration file to open')
    for name in ('forward', 'record', 'reverse', 'replay'):
        p = sub.add_parser(name, help=(
            'Record selected boundaries' if name in ('forward', 'record')
            else 'Replay or sample recorded outcomes'))
        p.add_argument('--config', help='JSON configuration file (see AGENTS.md)')
        p.add_argument('--cassette', help='Recording destination or replay source; overrides config.cassette')
        p.add_argument('--write-cassette', help='Separate fresh destination for reverse-mode recording fallback')
        p.add_argument('--runner', choices=['python', 'exec'], default='exec', help='python: instrument source; exec: HTTP proxy for a child command (default)')
        p.add_argument('--trace', action='append', default=[], help='Python name or module:qualified_name, with globs; repeatable')
        p.add_argument('--trace-file', action='append', default=[], help='Selector lines or JSON array; repeatable')
        p.add_argument('--scope', action='append', default=[], help='Import root to instrument, e.g. myapp; repeatable')
        p.add_argument('--trust-pickle', action='store_true', help='Allow loading trusted pickle outcomes (can execute code)')
        p.add_argument('--offline', choices=['policy', 'strict'], default='policy', help='policy: replay rules only; strict: unsupported, refuses to launch')
        p.add_argument('--network', choices=['none', 'proxy'], default=None, help='Network backend; exec always uses proxy; python cannot use proxy')
        p.add_argument('--sample', action='store_true', help='Sample recorded outcomes with replacement in reverse mode')
        p.add_argument('--sample-match', choices=['input', 'target'], help='Sample matching inputs or the entire target pool')
        p.add_argument('--seed', type=int, help='Seed for repeatable replay sampling')
        p.add_argument('program', nargs=argparse.REMAINDER, help='Target command after --; Python runner accepts a script or -m module')
    for name in ('validate', 'doctor', 'explain'):
        p = sub.add_parser(name, help={'validate': 'Check a JSON configuration',
                                      'doctor': 'Report configuration and runtime capabilities',
                                      'explain': 'Show the rule selected for an event JSON file'}[name])
        p.add_argument('--config', required=True)
        if name == 'explain':
            p.add_argument('--event', required=True)
            p.add_argument('--mode', choices=['forward', 'reverse'], default='reverse')
    p = sub.add_parser('inspect', help='Summarize a cassette without unpickling')
    p.add_argument('cassette')
    p = sub.add_parser('recover', help='Recover an interrupted recording journal')
    p.add_argument('journal')
    p.add_argument('--output', required=True)
    return root


def python_run(program, engine):
    program = list(program)
    if program and (Path(program[0]).name.startswith('python') or program[0] == sys.executable):
        program.pop(0)
    if not program:
        raise MockerError('Python runner needs a script or -m module')
    instrumentor = Instrumentor(engine)
    finder = Finder(instrumentor, engine.config.scope)
    finder.install()
    try:
        if program[0] == '-m':
            if len(program) < 2:
                raise MockerError('-m requires module name')
            module = program[1]
            sys.path.insert(0, os.getcwd())
            spec = importlib.util.find_spec(module)
            if spec and spec.submodule_search_locations is not None:
                module += '.__main__'
                spec = importlib.util.find_spec(module)
            if not spec or not hasattr(spec.loader, 'get_source'):
                raise MockerError('Python runner requires a source module')
            source = spec.loader.get_source(module)
            filename = spec.origin
            sys.argv = [filename, *program[2:]]
            package = module.rpartition('.')[0]
        else:
            filename = str(Path(program[0]).resolve())
            module = Path(filename).stem
            source = Path(filename).read_bytes()
            package, spec = '', None
            sys.argv = [filename, *program[1:]]
            sys.path.insert(0, str(Path(filename).parent))
        namespace = {'__name__': '__main__', '__file__': filename, '__package__': package, '__spec__': spec}
        import types
        main_module = types.ModuleType('__main__')
        main_module.__dict__.update(namespace)
        sys.modules['__main__'] = main_module
        instrumentor.execute(source, filename, module, main_module.__dict__)
    finally:
        sys.meta_path.remove(finder)


def run_proxy(program, config, mode, source, destination):
    if destination:
        target = Path(destination)
        if target.exists() or target.with_suffix('.journal.jsonl').exists():
            raise MockerError('recording destination or journal already exists')
    executable = shutil.which('mitmdump')
    if not executable:
        raise MockerError('install network support: pip install "mocker-record-replay[network]"')
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    env = os.environ.copy()
    env['MOCKER_HTTP_CONFIG'] = json.dumps({'config': config.data, 'mode': mode, 'source': source, 'destination': destination})
    addon = str(Path(__file__).with_name('proxy_addon.py'))
    command = [executable, '-q', '-s', addon, '--listen-host', '127.0.0.1', '--listen-port', str(port),
               '--set', 'connection_strategy=lazy', '--set', 'upstream_cert=false']
    proxy = subprocess.Popen(command, env=env)
    try:
        deadline = time.monotonic() + 20
        while True:
            if proxy.poll() is not None:
                raise MockerError('proxy exited before startup')
            try:
                with socket.create_connection(('127.0.0.1', port), timeout=0.2):
                    break
            except OSError:
                if time.monotonic() > deadline:
                    raise MockerError('proxy startup timed out')
                time.sleep(0.1)
        child_env = os.environ.copy()
        for key in ('HTTP_PROXY', 'HTTPS_PROXY', 'http_proxy', 'https_proxy', 'ALL_PROXY', 'all_proxy'):
            child_env[key] = f'http://127.0.0.1:{port}'
        for key in ('NO_PROXY', 'no_proxy'):
            child_env[key] = ''
        return subprocess.call(program, env=child_env)
    finally:
        proxy.terminate()
        try:
            proxy.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proxy.kill()
            proxy.wait()


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        if args.command == 'tui':
            try:
                from .tui import MockerApp
            except ModuleNotFoundError as exc:
                if exc.name == 'textual':
                    raise MockerError('install terminal UI support: pip install "mocker-record-replay[tui]"') from exc
                raise
            MockerApp(config_path=args.config).run()
            return 0
        if args.command == 'recover':
            Cassette.recover(args.journal, args.output)
            return 0
        if args.command == 'inspect':
            cassette = Cassette(args.cassette)
            print(json.dumps({'version': cassette.data['version'], 'complete': cassette.data['complete'],
                              'count': len(cassette.data['interactions']),
                              'targets': sorted({x['target'] for x in cassette.data['interactions']})}, indent=2))
            return 0
        if args.command in ('validate', 'doctor', 'explain'):
            config = Config.load(args.config)
            if args.command == 'explain':
                event = json.loads(Path(args.event).read_text())
                action, rule = config.resolve(event, args.mode)
                print(json.dumps({'selected': config.selected(event, args.mode), 'rule': rule, 'action': action,
                                  'lookup_key': fingerprint(event)}, indent=2))
            elif args.command == 'doctor':
                print(json.dumps({'config': 'valid', 'python': sys.version, 'proxy': bool(shutil.which('mitmdump')),
                                  'strict_offline': False, 'scope': config.scope}, indent=2))
            else:
                print('Configuration valid')
            return 0
        mode = 'forward' if args.command in ('forward', 'record') else 'reverse'
        if args.offline == 'strict':
            raise MockerError('strict offline enforcement is not implemented; target was not launched')
        data = json.loads(Path(args.config).read_text()) if args.config else {}
        path = args.cassette or data.get('cassette')
        if not path:
            raise MockerError('--cassette or config.cassette is required')
        source = Cassette(path, trust_pickle=args.trust_pickle) if mode == 'reverse' else None
        if source and not args.config:
            data = source.data.get('config', {})
        traces = args.trace + [x for p in args.trace_file for x in trace_file(p)]
        if traces:
            data['trace'] = data.get('trace', []) + traces
        if args.scope:
            data.setdefault('python', {})['scope'] = args.scope
        if args.sample or args.sample_match is not None or args.seed is not None:
            if mode != 'reverse':
                raise MockerError('sampling options require reverse mode')
            replay = data.setdefault('replay', {})
            if args.sample or args.sample_match is not None:
                replay['consume'] = 'sample'
            if args.sample_match is not None:
                replay['sample_match'] = args.sample_match
            if args.seed is not None:
                replay['seed'] = args.seed
        network = args.network or data.get('network', {}).get('backend', 'none')
        if network not in ('none', 'proxy'):
            raise MockerError('only explicit proxy network capture is implemented')
        if network == 'proxy' and args.runner == 'python':
            raise MockerError('mixed Python/proxy capture is not implemented; use separate boundary runs')
        if args.runner == 'exec' and network == 'none':
            network = 'proxy'
        if network == 'proxy':
            data.setdefault('targets', []).append({'kind': 'http'})
        config = Config(data)
        program = args.program[1:] if args.program[:1] == ['--'] else args.program
        if not program:
            raise MockerError('target command required after --')
        destination_path = path if mode == 'forward' else args.write_cassette
        if source and destination_path and Path(path).resolve() == Path(destination_path).resolve():
            raise MockerError('source and destination cassettes must differ')
        needs_write = config.on_miss == 'record' or any(r['action'].get('on_miss') == 'record' or r['action']['type'] == 'record'
                                                      for _, r in config.rules if mode in r.get('modes', [mode]))
        if mode == 'reverse' and needs_write and not destination_path:
            raise MockerError('record fallback requires --write-cassette')
        if network == 'proxy':
            return run_proxy(program, config, mode, str(Path(path).resolve()) if source else None,
                             str(Path(destination_path).resolve()) if destination_path else None)
        destination = Cassette(destination_path, write=True, config=config.data) if destination_path else None
        complete = False
        try:
            python_run(program, Engine(config, mode, source, destination))
            complete = True
            return 0
        except SystemExit as exc:
            complete = exc.code in (None, 0)
            raise
        finally:
            if destination:
                destination.close(complete)
    except (MockerError, OSError, ValueError, KeyError) as exc:
        print(f'mocker: {exc}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
