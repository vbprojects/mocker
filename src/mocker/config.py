"""Shared selectors, shorthand compilation, and deterministic rule resolution."""
import fnmatch
import json
from dataclasses import dataclass, field
from pathlib import Path
from .errors import MockerError


def selector(value):
    if isinstance(value, str):
        key = 'target' if ':' in value else 'name'
        return {'kind': 'python', key: {'glob': value} if any(c in value for c in '*?[') else value}
    if not isinstance(value, dict):
        raise MockerError('selector must be a string or object')
    return value


def pattern(expected, actual):
    if isinstance(expected, dict):
        return isinstance(actual, str) and fnmatch.fnmatchcase(actual, expected['glob'])
    return actual == expected


def matches(select, event):
    for key, expected in select.items():
        if key == 'where':
            for pointer, predicate in expected.items():
                value, exists = event, True
                try:
                    for part in pointer.lstrip('/').split('/'):
                        part = part.replace('~1', '/').replace('~0', '~')
                        value = value[int(part)] if isinstance(value, list) else value[part]
                except (KeyError, IndexError, TypeError, ValueError):
                    exists, value = False, None
                for op, operand in predicate.items():
                    if op == 'exists' and exists != operand:
                        return False
                    if op == 'eq' and (not exists or value != operand):
                        return False
                    if op == 'in' and (not exists or value not in operand):
                        return False
        elif not pattern(expected, event.get(key)):
            return False
    return True


@dataclass
class Config:
    data: dict = field(default_factory=dict)

    def __post_init__(self):
        self.data = json.loads(json.dumps(self.data))
        if self.data.get('version', 1) != 1:
            raise MockerError('unsupported configuration version')
        self.targets = [selector(x) for x in self.data.get('targets', []) + self.data.get('trace', [])]
        self.rules = list(self.data.get('rules', []))
        for mock in self.data.get('mock', []):
            for name, value in mock.items():
                self.rules.append({'select': selector(name), 'action': {'type': 'mock', 'outcome': {'return': value}}})
        self.scope = self.data.get('python', {}).get('scope', [])
        replay = self.data.get('replay', {})
        self.on_miss = replay.get('on_miss', 'error')
        self.consume = replay.get('consume', 'once')
        self.sample_match = replay.get('sample_match', 'input')
        self.seed = replay.get('seed')
        if self.on_miss not in ('error', 'live', 'record') or self.consume not in ('once', 'reuse', 'sample'):
            raise MockerError('invalid replay policy')
        if self.sample_match not in ('input', 'target'):
            raise MockerError('sample_match must be input or target')
        if self.seed is not None and type(self.seed) is not int:
            raise MockerError('replay.seed must be an integer or null')
        for rule in self.rules:
            rule['select'] = selector(rule['select'])
            action = rule['action']
            if action.get('type') not in ('record', 'replay', 'mock', 'live', 'block'):
                raise MockerError('unknown rule action')
            if action.get('on_miss', 'error') not in ('error', 'live', 'record'):
                raise MockerError('invalid on_miss')
            if action.get('consume', self.consume) not in ('once', 'reuse', 'sample'):
                raise MockerError('invalid rule consumption policy')
            if action.get('sample_match', self.sample_match) not in ('input', 'target'):
                raise MockerError('sample_match must be input or target')
            if any(x not in ('forward', 'reverse') for x in rule.get('modes', [])):
                raise MockerError('invalid rule modes')
            if action['type'] == 'mock':
                outcome = action.get('outcome', {})
                if len(set(outcome) & {'return', 'raise', 'response'}) != 1:
                    raise MockerError('mock requires exactly one return, raise, or response outcome')
        for item in self.targets + [r['select'] for r in self.rules]:
            allowed = {'kind', 'target', 'module', 'name', 'method', 'host', 'path', 'where'}
            if set(item) - allowed or item.get('kind') not in ('python', 'http'):
                raise MockerError('invalid selector fields or kind')
            for key, value in item.items():
                if key == 'where':
                    for path, predicate in value.items():
                        if not path.startswith('/') or not predicate or set(predicate) - {'eq', 'in', 'exists'}:
                            raise MockerError('invalid input predicate')
                elif isinstance(value, dict) and (set(value) != {'glob'} or not isinstance(value['glob'], str)):
                    raise MockerError('patterns require a glob string')
        self.rules = sorted(enumerate(self.rules), key=lambda x: (-x[1].get('priority', 0), x[0]))

    @classmethod
    def load(cls, path):
        return cls(json.loads(Path(path).read_text()))

    def selected(self, event, mode, definition=False):
        items = self.targets + [r['select'] for _, r in self.rules if mode in r.get('modes', [mode])]
        if definition:
            items = [{k: v for k, v in s.items() if k != 'where'} for s in items]
        return any(matches(s, event) for s in items)

    def resolve(self, event, mode):
        for index, rule in self.rules:
            if mode in rule.get('modes', [mode]) and matches(rule['select'], event):
                return rule['action'], rule.get('id', f'rule-{index}')
        return ({'type': 'record'} if mode == 'forward' else {'type': 'replay', 'on_miss': self.on_miss}), 'mode-default'
