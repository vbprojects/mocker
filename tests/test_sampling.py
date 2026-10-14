import json
import random

import pytest

from mocker import Cassette, Config, Engine
from mocker.errors import MockerError, ReplayMiss
from mocker.instrument import Instrumentor


def install(engine, source):
    ns = {}
    Instrumentor(engine).execute(source, 'demo.py', 'demo', ns)
    return ns


def capture(tmp_path, values=(10, 20, 30), *, inputs=None):
    config = Config({'trace': ['f']})
    store = Cassette(tmp_path / 'sample.json', write=True, config=config.data)
    ns = install(Engine(config, 'forward', destination=store), 'values = []\ndef f(x=1):\n return values.pop(0)')
    ns['values'].extend(values)
    for x in inputs or [1] * len(values):
        ns['f'](x)
    store.close()
    return store.path


def replay(path, **options):
    config = Config({'trace': ['f'], 'replay': {'consume': 'sample', **options}})
    engine = Engine(config, 'reverse', source=Cassette(path))
    return install(engine, 'def f(x=1):\n raise AssertionError("body executed")')['f']


def test_seeded_sampling_with_replacement_and_frequency(tmp_path):
    path = capture(tmp_path, values=[10, 10, 20])
    f = replay(path, seed=42)
    rng = random.Random(42)
    expected = [rng.choice([10, 10, 20]) for _ in range(100)]
    actual = [f() for _ in range(100)]
    assert actual == expected
    assert set(actual) == {10, 20}
    assert [replay(path, seed=42)() for _ in range(3)] == [expected[0]] * 3


def test_inputs_separate_pools_and_target_pool_opt_in(tmp_path):
    path = capture(tmp_path, values=[10, 20, 30], inputs=[1, 2, 3])
    f = replay(path, seed=1)
    assert [f(1) for _ in range(10)] == [10] * 10
    with pytest.raises(ReplayMiss):
        f(999)
    f = replay(path, seed=42, sample_match='target')
    draws = [f(999) for _ in range(20)]
    assert set(draws) == {10, 20, 30}


def test_sampled_mutable_returns_are_fresh(tmp_path):
    path = capture(tmp_path, values=[[1]])
    f = replay(path)
    f().append(99)
    assert f() == [1]


def test_target_sampling_can_ignore_non_json_arguments(tmp_path):
    path = capture(tmp_path, values=[10], inputs=[(1, 2)])
    f = replay(path, sample_match='target')
    assert f(object()) == 10
    with pytest.raises(MockerError):
        replay(path)(object())


def test_non_replayable_outcomes_are_excluded(tmp_path):
    path = capture(tmp_path, values=[lambda: None, 7])
    f = replay(path, sample_match='target')
    assert [f() for _ in range(20)] == [7] * 20
    path2 = tmp_path / 'unsupported.json'
    data = json.loads(path.read_text())
    data['interactions'] = data['interactions'][:1]
    path2.write_text(json.dumps(data))
    with pytest.raises(ReplayMiss):
        replay(path2)()


def test_pickle_sampling_still_requires_trust(tmp_path):
    path = capture(tmp_path, values=[(1, 2)])
    with pytest.raises(MockerError, match='trust-pickle'):
        replay(path)()
    config = Config({'trace': ['f'], 'replay': {'consume': 'sample'}})
    f = install(Engine(config, 'reverse', Cassette(path, trust_pickle=True)), 'def f(x=1):\n raise AssertionError')['f']
    assert f() == (1, 2)


def test_per_rule_sampling_and_default_fifo(tmp_path):
    path = capture(tmp_path)
    config = Config({'trace': ['f'], 'rules': [{'select': 'f', 'modes': ['reverse'],
        'action': {'type': 'replay', 'consume': 'sample', 'sample_match': 'target'}}], 'replay': {'seed': 42}})
    f = install(Engine(config, 'reverse', Cassette(path)), 'def f(x=1):\n raise AssertionError')['f']
    assert set(f(999) for _ in range(20)) == {10, 20, 30}
    config = Config({'trace': ['f']})
    f = install(Engine(config, 'reverse', Cassette(path)), 'def f(x=1):\n raise AssertionError')['f']
    assert [f() for _ in range(3)] == [10, 20, 30]
    with pytest.raises(ReplayMiss):
        f()


def test_sampled_parent_retires_descendants(tmp_path):
    config = Config({'trace': ['*']})
    store = Cassette(tmp_path / 'nested.json', write=True, config=config.data)
    install(Engine(config, 'forward', destination=store),
        'def child():\n return 1\ndef parent():\n return child()\nparent()\nchild()')
    store.close()
    config = Config({'trace': ['*'], 'rules': [{'select': 'parent', 'action': {'type': 'replay', 'consume': 'sample'}}]})
    ns = install(Engine(config, 'reverse', Cassette(store.path)),
        'def child():\n raise AssertionError\ndef parent():\n raise AssertionError')
    assert ns['parent']() == ns['parent']() == 1
    assert ns['child']() == 1
    with pytest.raises(ReplayMiss):
        ns['child']()


@pytest.mark.parametrize('data', [{'consume': 'random'}, {'seed': True}, {'seed': '42'}, {'sample_match': 'wildcard'}])
def test_invalid_sampling_config(data):
    with pytest.raises(MockerError):
        Config({'replay': data})


def test_sampling_preserves_recorded_exceptions(tmp_path):
    config = Config({'trace': ['f']})
    store = Cassette(tmp_path / 'exception.json', write=True, config=config.data)
    f = install(Engine(config, 'forward', destination=store), 'def f(x=1):\n raise ValueError("bad")')['f']
    with pytest.raises(ValueError):
        f()
    store.close()
    f = replay(store.path)
    for _ in range(3):
        with pytest.raises(ValueError, match='bad'):
            f()


def test_target_sample_hit_does_not_serialize_ignored_inputs(tmp_path):
    path = capture(tmp_path, values=[10])
    class Argument:
        def __reduce__(self):
            raise AssertionError('ignored input was pickled')
    destination = Cassette(tmp_path / 'extra.json', write=True)
    config = Config({'trace': ['f'], 'replay': {'consume': 'sample', 'sample_match': 'target', 'on_miss': 'record'}})
    f = install(Engine(config, 'reverse', Cassette(path), destination), 'def f(x=1):\n raise AssertionError')['f']
    assert f(Argument()) == 10
    assert not destination.data['interactions']
    assert not destination.blobs.exists()
