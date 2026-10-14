import asyncio
import json
import threading
from concurrent.futures import ThreadPoolExecutor
import pytest
from mocker import Cassette, Config, Engine
from mocker.errors import MockerError, ReplayMiss, Blocked
from mocker.instrument import Instrumentor
from mocker.storage import lossless_json


def setup(tmp_path, data=None):
    config = Config(data or {'trace': ['*']})
    store = Cassette(tmp_path / 'demo.json', write=True, config=config.data)
    engine = Engine(config, 'forward', destination=store)
    return config, store, engine


def namespace(engine, source):
    ns = {}
    Instrumentor(engine).execute(source, 'demo.py', 'demo', ns)
    return ns


def test_import_time_calls_and_body_skipped(tmp_path):
    config, store, engine = setup(tmp_path)
    ns = namespace(engine, 'def get_name(x=42):\n return "Ryan"\nresult = get_name()')
    assert ns['result'] == 'Ryan'
    store.close()
    replay = Engine(config, 'reverse', source=Cassette(store.path))
    ns = namespace(replay, 'def get_name(x=42):\n raise AssertionError("body ran")\nresult = get_name(x=42)')
    assert ns['result'] == 'Ryan'
    with pytest.raises(ReplayMiss):
        ns['get_name']()


def test_input_snapshot_occurrences_and_nested_subtrees(tmp_path):
    config, store, engine = setup(tmp_path)
    ns = namespace(engine, 'def child(x):\n return x\ndef parent(x):\n return child(x)\nparent(1)\nchild(1)')
    store.close()
    replay = Engine(config, 'reverse', source=Cassette(store.path))
    ns = namespace(replay, 'def child(x):\n raise AssertionError\ndef parent(x):\n raise AssertionError')
    assert ns['parent'](1) == 1
    assert ns['child'](1) == 1
    with pytest.raises(ReplayMiss):
        ns['child'](1)


def test_mutable_output_is_fresh_and_input_fingerprinted_before_mutation(tmp_path):
    config, store, engine = setup(tmp_path, {'trace': ['f'], 'replay': {'consume': 'reuse'}})
    ns = namespace(engine, 'def f(x):\n x.append(9)\n return [1]')
    ns['f']([])
    assert store.data['interactions'][0]['input']['arguments']['x'] == []
    store.close()
    ns = namespace(Engine(config, 'reverse', source=Cassette(store.path)), 'def f(x):\n raise AssertionError')
    ns['f']([]).append(2)
    assert ns['f']([]) == [1]


def test_async_and_exception(tmp_path):
    config, store, engine = setup(tmp_path)
    ns = namespace(engine, 'async def f(x):\n return x + 1\ndef fail():\n raise ValueError("bad")')
    assert asyncio.run(ns['f'](2)) == 3
    with pytest.raises(ValueError):
        ns['fail']()
    store.close()
    ns = namespace(Engine(config, 'reverse', source=Cassette(store.path)), 'async def f(x):\n raise AssertionError\ndef fail():\n raise AssertionError')
    assert asyncio.run(ns['f'](2)) == 3
    with pytest.raises(ValueError, match='bad'):
        ns['fail']()


def test_pickle_trust_digest_and_paths(tmp_path):
    config, store, engine = setup(tmp_path)
    ns = namespace(engine, 'def f():\n return (1, 2)')
    assert ns['f']() == (1, 2)
    store.close()
    event = store.data['interactions'][0]
    ref = event['outcome']['return']
    with pytest.raises(MockerError, match='trust-pickle'):
        Cassette(store.path).decode(ref)
    trusted = Cassette(store.path, trust_pickle=True)
    assert trusted.decode(ref) == (1, 2)
    with pytest.raises(MockerError, match='escapes'):
        trusted.decode({**ref, 'path': '../escape.pkl'})
    (tmp_path / ref['path']).write_bytes(b'corrupt')
    with pytest.raises(MockerError, match='digest'):
        trusted.decode(ref)


def test_lossless_json_shared_refs_and_types():
    child = []
    assert not lossless_json([child, child])
    assert not lossless_json((1, 2))
    assert not lossless_json({1: 'one'})
    assert not lossless_json(float('nan'))


def test_rule_priority_shorthand_and_predicates():
    config = Config({'trace': ['f'], 'mock': [{'get_name_*': 'Ryan'}], 'rules': [
        {'id': 'special', 'priority': 1, 'select': {'kind': 'python', 'name': 'get_name_user',
         'where': {'/input/arguments/x': {'eq': 42}}}, 'action': {'type': 'block'}}]})
    event = {'kind': 'python', 'name': 'get_name_user', 'input': {'arguments': {'x': 42}}}
    assert config.resolve(event, 'reverse')[1] == 'special'
    event['input']['arguments']['x'] = 1
    assert config.resolve(event, 'reverse')[0]['outcome']['return'] == 'Ryan'


def test_mode_scoping_and_mock_forward(tmp_path):
    config, store, engine = setup(tmp_path, {'mock': [{'get_name_*': 'Ryan'}]})
    ns = namespace(engine, 'def get_name_user():\n raise AssertionError')
    assert ns['get_name_user']() == 'Ryan'
    assert not store.data['interactions']


def test_staticmethod_and_classmethod_mock(tmp_path):
    config, store, engine = setup(tmp_path, {'mock': [{'f': 3}]})
    ns = namespace(engine, 'class C:\n @classmethod\n def f(cls):\n  raise AssertionError\nclass D:\n @staticmethod\n def f():\n  raise AssertionError')
    assert ns['C'].f() == ns['D'].f() == 3


def test_nonreplayable_output_recorded(tmp_path):
    config, store, engine = setup(tmp_path)
    ns = namespace(engine, 'def f():\n return lambda x: x')
    ns['f']()
    assert store.data['interactions'][0]['replayability'] == 'unsupported'


def test_partial_record_source_not_modified(tmp_path):
    config, store, engine = setup(tmp_path)
    ns = namespace(engine, 'def f(x):\n return x')
    ns['f'](1)
    store.close()
    baseline = store.path.read_bytes()
    config = Config({'trace': ['f'], 'replay': {'on_miss': 'record'}})
    destination = Cassette(tmp_path / 'new.json', write=True)
    ns = namespace(Engine(config, 'reverse', Cassette(store.path), destination), 'def f(x):\n return x')
    assert ns['f'](1) == 1
    assert ns['f'](2) == 2
    destination.close()
    assert store.path.read_bytes() == baseline
    assert len(destination.data['interactions']) == 1


def test_atomic_concurrent_consumption(tmp_path):
    config, store, engine = setup(tmp_path)
    ns = namespace(engine, 'def f():\n return 1')
    for _ in range(20):
        ns['f']()
    store.close()
    ns = namespace(Engine(config, 'reverse', Cassette(store.path)), 'def f():\n raise AssertionError')
    with ThreadPoolExecutor(max_workers=5) as pool:
        assert list(pool.map(lambda _: ns['f'](), range(20))) == [1] * 20
    with pytest.raises(ReplayMiss):
        ns['f']()


def test_recover_truncated_journal_and_overwrite_refused(tmp_path):
    config, store, engine = setup(tmp_path)
    namespace(engine, 'def f():\n return 1\nf()')
    with store.journal.open('a') as stream:
        stream.write('{')
    Cassette.recover(store.journal, store.path)
    assert not Cassette(store.path).data['complete']
    assert len(Cassette(store.path).data['interactions']) == 1
    with pytest.raises(MockerError):
        Cassette(store.path, write=True)


@pytest.mark.parametrize('data', [{'version': 2}, {'replay': {'on_miss': 'silent'}},
                                {'trace': [{'kind': 'python', 'nmae': 'f'}]},
                                {'rules': [{'select': 'f', 'action': {'type': 'wat'}}]}])
def test_invalid_config(data):
    with pytest.raises(MockerError):
        Config(data)


def test_mock_mutable_return_is_fresh(tmp_path):
    config, store, engine = setup(tmp_path, {'mock': [{'f': [1]}]})
    ns = namespace(engine, 'def f():\n raise AssertionError')
    ns['f']().append(2)
    assert ns['f']() == [1]


def test_non_json_input_is_captured_without_claiming_stable_matching(tmp_path):
    config, store, engine = setup(tmp_path)
    ns = namespace(engine, 'def f(x):\n return 7')
    assert ns['f']((1, 2)) == 7
    event = store.data['interactions'][0]
    assert event['input']['codec'] == 'pickle'
    assert event['replayability'] == 'unsupported'


def test_non_picklable_input_does_not_prevent_forward_execution(tmp_path):
    config, store, engine = setup(tmp_path)
    ns = namespace(engine, 'def f(x):\n return 7')
    assert ns['f'](lambda: None) == 7
    assert store.data['interactions'][0]['input']['codec'] == 'unsupported'
