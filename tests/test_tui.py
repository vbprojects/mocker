import asyncio
import json
import os
from pathlib import Path
import sys

import pytest

pytest.importorskip('textual')
from textual.widgets import Input, Select, Static, TextArea, TabbedContent, RichLog
from mocker.errors import MockerError
from mocker.tui import MockerApp, ConfirmScreen, parse_document, run_arguments


def test_save_load_and_preserve_advanced_selectors(tmp_path):
    async def scenario():
        data = {'version': 1, 'cassette': 'test.json', 'trace': ['f', {'kind': 'python', 'name': 'g'}],
                'targets': [{'kind': 'http', 'host': 'example.com'}], 'custom': {'keep': True}}
        config = tmp_path / 'mocker.json'
        config.write_text(json.dumps(data))
        app = MockerApp(str(config))
        async with app.run_test(size=(100, 35)) as pilot:
            await pilot.pause()
            app.query_one('#trace', TextArea).load_text('f\nget_name_*')
            app.query_one('#mock', TextArea).load_text('[{"get_name_*": "Ryan"}]')
            app.query_one('#program', Input).value = 'python app.py'
            await pilot.click('#save')
            await pilot.pause()
            result = json.loads(config.read_text())
            assert result['targets'] == data['targets']
            assert result['custom'] == data['custom']
            assert result['trace'] == ['f', data['trace'][1], 'get_name_*']
            assert result['mock'] == [{'get_name_*': 'Ryan'}]
            assert result['tui']['program'] == 'python app.py'
            assert not app.dirty
            app.query_one('#trace', TextArea).load_text('changed')
            app.action_load()
            await pilot.pause()
            assert isinstance(app.screen, ConfirmScreen)
            await pilot.click('#cancel')
            await pilot.pause()
            assert app.query_one('#trace', TextArea).text == 'changed'
    asyncio.run(scenario())


def test_json_edit_sync_and_invalid_save(tmp_path):
    async def scenario():
        app = MockerApp()
        async with app.run_test(size=(100, 35)) as pilot:
            app.query_one('#config-path', Input).value = str(tmp_path / 'config.json')
            tabs = app.query_one('#tabs', TabbedContent)
            tabs.active = 'json'
            await pilot.pause()
            app.query_one('#json-editor', TextArea).load_text('{broken')
            app.action_save()
            assert not (tmp_path / 'config.json').exists()
            tabs.active = 'run'
            await pilot.pause()
            assert tabs.active == 'json'
            document = {'cassette': 'demo.json', 'mock': [{'get_name_*': 'Ryan'}]}
            app.query_one('#json-editor', TextArea).load_text(json.dumps(document))
            tabs.active = 'form'
            await pilot.pause()
            assert json.loads(app.query_one('#mock', TextArea).text) == document['mock']
            app.action_save()
            assert json.loads((tmp_path / 'config.json').read_text())['mock'] == document['mock']
    asyncio.run(scenario())


async def finish(app):
    for _ in range(200):
        if not app.running:
            return
        await asyncio.sleep(0.02)
    pytest.fail('Program did not finish')


def test_forward_reverse_through_ui(tmp_path):
    script = tmp_path / 'program.py'
    script.write_text('def get_name():\n return "Ryan"\nprint(get_name())')
    async def scenario():
        app = MockerApp()
        async with app.run_test(size=(100, 35)) as pilot:
            app.query_one('#cwd', Input).value = str(tmp_path)
            app.query_one('#cassette', Input).value = 'capture.json'
            app.query_one('#program', Input).value = 'python program.py'
            app.query_one('#trace', TextArea).load_text('get_name')
            app.query_one('#tabs', TabbedContent).active = 'run'
            await pilot.pause()
            await pilot.click('#run-button')
            await finish(app)
            assert app.last_exit == 0
            assert 'Ryan' in ''.join(line.text for line in app.query_one('#output', RichLog).lines)
            assert (tmp_path / 'capture.json').exists()
            script.write_text('def get_name():\n raise AssertionError("body ran")\nprint(get_name())')
            app.query_one('#mode', Select).value = 'reverse'
            await pilot.click('#run-button')
            await finish(app)
            assert app.last_exit == 0
            assert 'Finished' in str(app.query_one('#status', Static).render())
    asyncio.run(scenario())


def test_stdin_and_stop(tmp_path):
    (tmp_path / 'program.py').write_text('print("READY", flush=True)\nprint(input())')
    async def scenario():
        app = MockerApp()
        async with app.run_test(size=(100, 35)) as pilot:
            app.query_one('#cwd', Input).value = str(tmp_path)
            app.query_one('#program', Input).value = 'python program.py'
            app.query_one('#tabs', TabbedContent).active = 'run'
            await pilot.pause()
            app.action_run()
            for _ in range(100):
                if app.process:
                    break
                await asyncio.sleep(.02)
            app.query_one('#stdin', Input).value = 'hello'
            await pilot.click('#send')
            await finish(app)
            assert app.last_exit == 0
            assert 'hello' in ''.join(line.text for line in app.query_one('#output', RichLog).lines)
            (tmp_path / 'program.py').write_text('import time\nprint("READY", flush=True)\ntime.sleep(60)')
            # Each forward recording needs a fresh path.
            app.query_one('#cassette', Input).value = 'second.json'
            app.action_run()
            for _ in range(100):
                if app.process:
                    break
                await asyncio.sleep(.02)
            app.action_stop()
            await finish(app)
            assert app.last_exit != 0
            assert app.process is None
            assert not app.query_one('#run-button').disabled
    asyncio.run(scenario())


def test_arguments_preserve_quoting_and_do_not_use_shell(tmp_path):
    args, cwd = run_arguments({'cassette': 'a.json'}, {
        'cwd': str(tmp_path), 'program': 'python "file with spaces.py" "$(touch nope)"',
        'mode': 'reverse', 'runner': 'python', 'write_cassette': 'new.json', 'trust_pickle': True},
        tmp_path / 'snapshot.json')
    assert args[-3:] == ['python', 'file with spaces.py', '$(touch nope)']
    assert '--trust-pickle' in args
    assert str(tmp_path / 'new.json') in args
    assert cwd == tmp_path


def test_small_terminal_run_controls_visible():
    async def scenario():
        app = MockerApp()
        async with app.run_test(size=(80, 24)) as pilot:
            app.query_one('#tabs', TabbedContent).active = 'run'
            await pilot.pause()
            for widget in ('#run-button', '#output', '#stdin'):
                region = app.query_one(widget).region
                assert region.bottom <= 23, (widget, region)
    asyncio.run(scenario())


def test_new_edits_require_discard_confirmation():
    async def scenario():
        app = MockerApp()
        async with app.run_test() as pilot:
            app.new_document()
            app.query_one('#cassette', Input).value = 'changed.json'
            app.action_request_quit()
            await pilot.pause()
            assert isinstance(app.screen, ConfirmScreen)
            await pilot.click('#cancel')
    asyncio.run(scenario())


@pytest.mark.parametrize('data', [{'tui': {'mode': 'bad'}}, {'tui': {'program': 7}},
                                {'python': {'scope': 'app'}}, {'cassette': 42}])
def test_invalid_ui_settings_rejected(data):
    with pytest.raises(MockerError):
        parse_document(json.dumps(data))
