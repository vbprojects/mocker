"""Optional Textual configuration workbench and asynchronous process runner."""
from __future__ import annotations

import asyncio
import codecs
import json
import os
from pathlib import Path
import shlex
import signal
import sys
import tempfile

from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Checkbox, Footer, Header, Input, Label, RichLog, Select, Static, TabbedContent, TabPane, TextArea

from .config import Config
from .errors import MockerError
from .storage import atomic_write

DEFAULT_CONFIG = {
    'version': 1,
    'cassette': 'cassettes/session.json',
    'python': {'scope': []},
    'trace': [],
    'mock': [],
    'rules': [],
    'replay': {'on_miss': 'error', 'consume': 'once'},
}


def parse_document(text: str) -> dict:
    data = json.loads(text)
    if not isinstance(data, dict):
        raise MockerError('Configuration must be a JSON object')
    Config(data)
    if 'cassette' in data and not isinstance(data['cassette'], str):
        raise MockerError('cassette must be a path string')
    scope = data.get('python', {}).get('scope', [])
    if not isinstance(scope, list) or any(not isinstance(x, str) for x in scope):
        raise MockerError('python.scope must be a list of import roots')
    settings = data.get('tui', {})
    if not isinstance(settings, dict):
        raise MockerError('tui settings must be an object')
    for field in ('program', 'cwd', 'write_cassette'):
        if field in settings and not isinstance(settings[field], str):
            raise MockerError(f'tui.{field} must be a string')
    if settings.get('mode', 'forward') not in ('forward', 'reverse'):
        raise MockerError('tui.mode must be forward or reverse')
    if settings.get('runner', 'python') not in ('python', 'exec'):
        raise MockerError('tui.runner must be python or exec')
    if 'trust_pickle' in settings and not isinstance(settings['trust_pickle'], bool):
        raise MockerError('tui.trust_pickle must be a boolean')
    return data


def string_list(text: str) -> list[str]:
    return [line.strip() for line in text.splitlines() if line.strip() and not line.lstrip().startswith('#')]


def resolve_path(value: str, base: Path) -> Path:
    path = Path(value).expanduser()
    return path.resolve() if path.is_absolute() else (base / path).resolve()


def run_arguments(data: dict, settings: dict, snapshot: Path) -> tuple[list[str], Path]:
    """Build argv without a shell; relative cassette paths follow the program cwd."""
    Config(data)
    cwd = Path(settings['cwd']).expanduser().resolve()
    if not cwd.is_dir():
        raise MockerError('Working directory does not exist')
    program = shlex.split(settings['program'])
    if not program:
        raise MockerError('Enter a program command')
    if settings['mode'] not in ('forward', 'reverse') or settings['runner'] not in ('python', 'exec'):
        raise MockerError('Invalid mode or runner')
    cassette = data.get('cassette', '')
    if not isinstance(cassette, str) or not cassette.strip():
        raise MockerError('Enter a cassette path')
    args = [sys.executable, '-u', '-m', 'mocker', settings['mode'], '--runner', settings['runner'],
            '--config', str(snapshot), '--cassette', str(resolve_path(cassette, cwd))]
    if settings.get('write_cassette'):
        args += ['--write-cassette', str(resolve_path(settings['write_cassette'], cwd))]
    if settings.get('trust_pickle'):
        args.append('--trust-pickle')
    return args + ['--', *program], cwd


class ConfirmScreen(ModalScreen[bool]):
    CSS = """
    ConfirmScreen { align: center middle; }
    #confirm-dialog { width: 64; height: auto; padding: 1 2; border: thick $accent; background: $surface; }
    #confirm-dialog Horizontal { height: 3; align: right middle; }
    """

    def __init__(self, message: str):
        super().__init__()
        self.message = message

    def compose(self) -> ComposeResult:
        with Vertical(id='confirm-dialog'):
            yield Label(self.message)
            with Horizontal():
                yield Button('Cancel', id='cancel')
                yield Button('Discard changes', id='discard', variant='warning')

    @on(Button.Pressed)
    def answer(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == 'discard')


class MockerApp(App):
    TITLE = 'mocker'
    SUB_TITLE = 'Configuration & record/replay'
    BINDINGS = [Binding('ctrl+s', 'save', 'Save', priority=True),
                Binding('ctrl+o', 'load', 'Open', priority=True),
                Binding('f5', 'run', 'Run', priority=True),
                Binding('f6', 'stop', 'Stop', priority=True),
                Binding('ctrl+c', 'stop', 'Stop', priority=True, show=False),
                Binding('ctrl+q', 'request_quit', 'Quit', priority=True)]
    CSS = """
    Screen { background: $background; }
    #filebar { height: 3; padding: 0 1; }
    #config-path { width: 1fr; }
    #filebar Button { min-width: 9; margin-left: 1; }
    #status { height: 1; padding: 0 2; color: $text-muted; }
    TabbedContent { height: 1fr; }
    TabPane { padding: 0 1; }
    .field { height: auto; margin-bottom: 1; }
    .field Label { height: 1; color: $text-muted; }
    .field TextArea { height: 6; }
    .row { height: auto; }
    .row .field { width: 1fr; margin-right: 2; }
    #json-editor { height: 1fr; }
    #run-settings { height: 1fr; min-height: 3; }
    #output { height: 1fr; min-height: 3; border: round $primary; }
    #run-controls, #stdin-controls { height: 3; margin-bottom: 1; }
    #run-controls Button, #stdin-controls Button { margin-right: 1; }
    #stdin { width: 1fr; }
    #trust-pickle { height: 3; }
    #run-help { height: auto; margin-bottom: 1; color: $text-muted; }
    """

    def __init__(self, config_path: str | None = None):
        super().__init__()
        self.start_directory = Path.cwd()
        self.initial_path = config_path
        self.base = json.loads(json.dumps(DEFAULT_CONFIG))
        self.saved_state: str | None = None
        self.process: asyncio.subprocess.Process | None = None
        self.running = False
        self.last_exit: int | None = None
        self.last_tab = 'form'
        self.stopping = False

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal(id='filebar'):
            yield Input(self.initial_path or 'mocker.json', placeholder='Configuration file path', id='config-path')
            yield Button('New', id='new')
            yield Button('Open', id='load')
            yield Button('Save', id='save', variant='primary')
            yield Button('Validate', id='validate')
        yield Static('New configuration · Ctrl+S save · F5 run · F6 stop', id='status', markup=False)
        with TabbedContent(initial='form', id='tabs'):
            with TabPane('Configuration', id='form'):
                with VerticalScroll():
                    with Vertical(classes='field'):
                        yield Label('Cassette path (relative to the program working directory)')
                        yield Input(DEFAULT_CONFIG['cassette'], id='cassette')
                    with Horizontal(classes='row'):
                        with Vertical(classes='field'):
                            yield Label('Python scopes · one import root per line')
                            yield TextArea('', id='scope')
                        with Vertical(classes='field'):
                            yield Label('Trace functions · exact names or globs, one per line')
                            yield TextArea('', id='trace')
                    with Horizontal(classes='row'):
                        with Vertical(classes='field'):
                            yield Label('On replay miss')
                            yield Select([('Fail', 'error'), ('Run live', 'live'), ('Record to a new cassette', 'record')],
                                         value='error', allow_blank=False, id='on-miss')
                        with Vertical(classes='field'):
                            yield Label('Recorded occurrences')
                            yield Select([('Consume once', 'once'), ('Reuse first outcome', 'reuse')],
                                         value='once', allow_blank=False, id='consume')
                    with Vertical(classes='field'):
                        yield Label('Fixed mocks · JSON list, e.g. [{"get_name_*": "Ryan"}]')
                        yield TextArea('[]', language='json', id='mock')
                    with Vertical(classes='field'):
                        yield Label('Rules · JSON list (selectors, priorities, modes and actions)')
                        yield TextArea('[]', language='json', id='rules')
                    yield Static('Use the JSON tab for HTTP selectors, trace selector objects, and other advanced options.', markup=False)
            with TabPane('JSON', id='json'):
                yield TextArea(json.dumps(self.base, indent=2), language='json', show_line_numbers=True, id='json-editor')
            with TabPane('Run & output', id='run'):
                with VerticalScroll(id='run-settings'):
                    with Horizontal(classes='row'):
                        with Vertical(classes='field'):
                            yield Label('Mode')
                            yield Select([('Forward · record', 'forward'), ('Reverse · replay', 'reverse')],
                                         value='forward', allow_blank=False, id='mode')
                        with Vertical(classes='field'):
                            yield Label('Runner')
                            yield Select([('Python functions', 'python'), ('Executable / HTTP proxy', 'exec')],
                                         value='python', allow_blank=False, id='runner')
                    with Vertical(classes='field'):
                        yield Label('Program command · quoted arguments supported, no shell expansion')
                        yield Input('', placeholder='python -m myapp  or  curl https://example.com', id='program')
                    with Horizontal(classes='row'):
                        with Vertical(classes='field'):
                            yield Label('Program working directory')
                            yield Input(str(self.start_directory), id='cwd')
                        with Vertical(classes='field'):
                            yield Label('New cassette for partial replay (optional)')
                            yield Input('', id='write-cassette')
                    yield Checkbox('Trust pickle blobs during replay (trusted recordings only)', id='trust-pickle')
                    yield Static('Run uses the current editor contents. Save writes config and run settings. '
                                 'Output is streamed; use the input field for line-based stdin.', id='run-help', markup=False)
                with Horizontal(id='run-controls'):
                    yield Button('Run', id='run-button', variant='success')
                    yield Button('Stop', id='stop', variant='error', disabled=True)
                    yield Button('Clear output', id='clear')
                yield RichLog(id='output', highlight=False, markup=False, wrap=True, max_lines=3000)
                with Horizontal(id='stdin-controls'):
                    yield Input('', placeholder='Send a line to the running program', id='stdin', disabled=True)
                    yield Button('Send', id='send', disabled=True)
                    yield Button('Close stdin', id='close-stdin', disabled=True)
        yield Footer()

    def on_mount(self) -> None:
        self.saved_state = self.state_signature()
        if self.initial_path:
            self.load_document()

    def status(self, message: str, error: bool = False) -> None:
        self.query_one('#status', Static).update(message)
        if error:
            self.notify(message, severity='error', timeout=8)

    def settings(self) -> dict:
        return {
            'program': self.query_one('#program', Input).value,
            'cwd': self.query_one('#cwd', Input).value,
            'mode': self.query_one('#mode', Select).value,
            'runner': self.query_one('#runner', Select).value,
            'write_cassette': self.query_one('#write-cassette', Input).value,
            'trust_pickle': self.query_one('#trust-pickle', Checkbox).value,
        }

    def form_document(self) -> dict:
        data = json.loads(json.dumps(self.base))
        data['cassette'] = self.query_one('#cassette', Input).value.strip()
        data.setdefault('python', {})['scope'] = string_list(self.query_one('#scope', TextArea).text)
        # Preserve structured trace selectors loaded through JSON, including their relative order.
        names = iter(string_list(self.query_one('#trace', TextArea).text))
        trace = []
        for item in self.base.get('trace', []):
            if isinstance(item, dict):
                trace.append(item)
            else:
                replacement = next(names, None)
                if replacement is not None:
                    trace.append(replacement)
        trace.extend(names)
        data['trace'] = trace
        for field in ('mock', 'rules'):
            values = json.loads(self.query_one('#' + field, TextArea).text)
            if not isinstance(values, list):
                raise MockerError(f'{field} must be a JSON list')
            data[field] = values
        data.setdefault('replay', {}).update(on_miss=self.query_one('#on-miss', Select).value,
                                             consume=self.query_one('#consume', Select).value)
        Config(data)
        return data

    def current_document(self) -> dict:
        if self.last_tab == 'json':
            data = parse_document(self.query_one('#json-editor', TextArea).text)
        else:
            data = self.form_document()
        data['tui'] = self.settings()
        return data

    def state_signature(self) -> str:
        try:
            return json.dumps(self.current_document(), sort_keys=True)
        except Exception:
            # Invalid edits must still trigger the unsaved-changes guard.
            return 'invalid:' + repr([(w.id, getattr(w, 'text', getattr(w, 'value', None)))
                                      for w in self.query('Input, TextArea, Select, Checkbox')])

    @property
    def dirty(self) -> bool:
        return self.saved_state is not None and self.state_signature() != self.saved_state

    def populate(self, data: dict, *, restore_settings: bool = True) -> None:
        self.base = json.loads(json.dumps(data))
        self.query_one('#cassette', Input).value = data.get('cassette', '')
        self.query_one('#scope', TextArea).load_text('\n'.join(data.get('python', {}).get('scope', [])))
        self.query_one('#trace', TextArea).load_text('\n'.join(x for x in data.get('trace', []) if isinstance(x, str)))
        for field in ('mock', 'rules'):
            self.query_one('#' + field, TextArea).load_text(json.dumps(data.get(field, []), indent=2))
        self.query_one('#on-miss', Select).value = data.get('replay', {}).get('on_miss', 'error')
        self.query_one('#consume', Select).value = data.get('replay', {}).get('consume', 'once')
        self.query_one('#json-editor', TextArea).load_text(json.dumps(data, indent=2))
        if restore_settings:
            settings = data.get('tui', {})
            for field, default in [('program', ''), ('cwd', str(self.start_directory)), ('write_cassette', '')]:
                self.query_one('#' + field.replace('_', '-'), Input).value = settings.get(field, default)
            self.query_one('#mode', Select).value = settings.get('mode', 'forward')
            self.query_one('#runner', Select).value = settings.get('runner', 'python')
            self.query_one('#trust-pickle', Checkbox).value = settings.get('trust_pickle', False)

    @on(TabbedContent.TabActivated, '#tabs')
    def tab_changed(self, event: TabbedContent.TabActivated) -> None:
        active = event.pane.id
        if active == self.last_tab:
            return
        try:
            if self.last_tab == 'json':
                data = parse_document(self.query_one('#json-editor', TextArea).text)
                self.populate(data, restore_settings=False)
            elif active == 'json':
                self.query_one('#json-editor', TextArea).load_text(json.dumps(self.current_document(), indent=2))
            self.last_tab = active
        except (MockerError, ValueError, TypeError, KeyError, AttributeError) as exc:
            self.status(f'Cannot switch tabs: {exc}', error=True)
            self.query_one('#tabs', TabbedContent).active = self.last_tab

    def file_path(self) -> Path:
        value = self.query_one('#config-path', Input).value.strip()
        if not value:
            raise MockerError('Enter a configuration file path')
        return resolve_path(value, self.start_directory)

    def guarded(self, callback) -> None:
        if self.dirty:
            self.push_screen(ConfirmScreen('Discard unsaved configuration or run setting changes?'),
                             lambda answer: callback() if answer else None)
        else:
            callback()

    def load_document(self) -> None:
        try:
            path = self.file_path()
            data = parse_document(path.read_text())
            self.populate(data)
            self.last_tab = 'form'
            self.query_one('#tabs', TabbedContent).active = 'form'
            self.saved_state = self.state_signature()
            self.status(f'Opened {path}')
        except (OSError, ValueError, MockerError, TypeError, KeyError, AttributeError) as exc:
            self.status(f'Open failed: {exc}', error=True)

    def action_load(self) -> None:
        self.guarded(self.load_document)

    def new_document(self) -> None:
        self.populate(DEFAULT_CONFIG)
        self.last_tab = 'form'
        self.query_one('#tabs', TabbedContent).active = 'form'
        self.saved_state = self.state_signature()
        self.status('New configuration · choose a file path and Save')

    def action_save(self) -> None:
        try:
            data = self.current_document()
            path = self.file_path()
            atomic_write(path, (json.dumps(data, indent=2, allow_nan=False) + '\n').encode())
            self.base = data
            self.saved_state = self.state_signature()
            self.status(f'Saved {path}')
        except (OSError, ValueError, MockerError, TypeError, KeyError, AttributeError) as exc:
            self.status(f'Save failed: {exc}', error=True)

    @on(Button.Pressed)
    def button_pressed(self, event: Button.Pressed) -> None:
        actions = {'new': lambda: self.guarded(self.new_document), 'load': self.action_load,
                   'save': self.action_save, 'run-button': self.action_run, 'stop': self.action_stop,
                   'clear': lambda: self.query_one('#output', RichLog).clear(), 'send': self.send_input,
                   'close-stdin': self.close_input}
        if event.button.id == 'validate':
            try:
                self.current_document()
                self.status('Configuration valid')
            except (ValueError, MockerError, TypeError, KeyError, AttributeError) as exc:
                self.status(f'Validation failed: {exc}', error=True)
        elif event.button.id in actions:
            actions[event.button.id]()

    def set_running(self, value: bool) -> None:
        self.running = value
        self.query_one('#run-button', Button).disabled = value
        self.query_one('#stop', Button).disabled = not value
        for widget in ('#stdin', '#send', '#close-stdin'):
            self.query_one(widget).disabled = not value

    def action_run(self) -> None:
        if self.running:
            return
        try:
            data = self.current_document()
            settings = self.settings()
            settings['cwd'] = str(resolve_path(settings['cwd'], self.start_directory))
            # Preflight without side effects before starting the worker.
            run_arguments(data, settings, Path('snapshot.json'))
        except (ValueError, MockerError, TypeError, KeyError, AttributeError) as exc:
            self.status(f'Run failed: {exc}', error=True)
            return
        self.query_one('#tabs', TabbedContent).active = 'run'
        self.set_running(True)
        self.last_exit = None
        self.stopping = False
        self.run_worker(self.execute(data, settings), name='mocker-run', exit_on_error=False)

    async def execute(self, data: dict, settings: dict) -> None:
        log = self.query_one('#output', RichLog)
        try:
            with tempfile.TemporaryDirectory(prefix='mocker-tui-') as folder:
                snapshot = Path(folder) / 'config.json'
                snapshot.write_text(json.dumps(data, indent=2))
                args, cwd = run_arguments(data, settings, snapshot)
                log.write(f'\n$ mocker {settings["mode"]} --runner {settings["runner"]} -- {settings["program"]}')
                self.status(f'Running {settings["mode"]} · {cwd}')
                env = os.environ.copy()
                env['PYTHONUNBUFFERED'] = '1'
                options = {'start_new_session': True} if os.name == 'posix' else {}
                self.process = await asyncio.create_subprocess_exec(*args, cwd=cwd, env=env,
                    stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT, **options)
                if self.stopping:
                    await self.terminate_process()
                decoder = codecs.getincrementaldecoder('utf-8')(errors='replace')
                pending = ''
                while chunk := await self.process.stdout.read(4096):
                    pending += decoder.decode(chunk)
                    lines = pending.split('\n')
                    pending = lines.pop()
                    for line in lines:
                        log.write(line.rstrip('\r'))
                    # Bound partial lines and show prompts immediately rather than waiting for newline.
                    if pending:
                        log.write(pending)
                        pending = ''
                tail = decoder.decode(b'', final=True)
                if tail:
                    log.write(tail)
                self.last_exit = await self.process.wait()
                log.write(f'Process exited with code {self.last_exit}')
                self.status(f'{"Stopped" if self.stopping else "Finished"} · exit code {self.last_exit}')
        except asyncio.CancelledError:
            await self.terminate_process()
            raise
        except (OSError, ValueError, MockerError) as exc:
            log.write(f'Run failed: {exc}')
            self.status(f'Run failed: {exc}', error=True)
        finally:
            self.process = None
            self.set_running(False)

    def action_stop(self) -> None:
        if not self.running or self.stopping:
            return
        self.stopping = True
        self.status('Stopping program and descendants…')
        self.run_worker(self.terminate_process(), name='mocker-stop', exit_on_error=False)

    async def terminate_process(self) -> None:
        process = self.process
        if process is None:
            return
        if os.name == 'posix':
            try:
                os.killpg(process.pid, signal.SIGINT)
            except ProcessLookupError:
                return
            # Drain happens in execute; allow normal cassette finalization before escalation.
            try:
                await asyncio.wait_for(process.wait(), 3)
            except asyncio.TimeoutError:
                pass
            # Kill remaining descendants even if their parent has already exited.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        elif process.returncode is None:
            killer = await asyncio.create_subprocess_exec('taskkill', '/PID', str(process.pid), '/T', '/F',
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
            await killer.wait()
        if process.returncode is None:
            await process.wait()

    @on(Input.Submitted, '#stdin')
    def input_submitted(self) -> None:
        self.send_input()

    def send_input(self) -> None:
        if self.process and self.process.stdin and not self.process.stdin.is_closing():
            value = self.query_one('#stdin', Input).value
            try:
                self.process.stdin.write((value + '\n').encode())
            except (BrokenPipeError, ConnectionResetError):
                self.status('Program stdin is closed')
                return
            self.query_one('#stdin', Input).value = ''
            self.run_worker(self.drain_input(), exit_on_error=False)

    async def drain_input(self) -> None:
        try:
            if self.process and self.process.stdin:
                await self.process.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            self.status('Program stdin is closed')

    def close_input(self) -> None:
        if self.process and self.process.stdin:
            self.process.stdin.close()
        for widget in ('#stdin', '#send', '#close-stdin'):
            self.query_one(widget).disabled = True

    def action_request_quit(self) -> None:
        if self.running:
            self.status('Stop the running program before quitting', error=True)
            return
        self.guarded(self.exit)

    async def on_unmount(self) -> None:
        await self.terminate_process()
