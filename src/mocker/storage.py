"""Typed storage with verified blobs and an append-only recovery journal."""
import hashlib
import json
import math
import os
import pickle
import tempfile
import threading
from pathlib import Path
from .errors import MockerError


def lossless_json(value, seen=None):
    seen = set() if seen is None else seen
    if type(value) in (str, bool, int, type(None)):
        return True
    if type(value) is float:
        return math.isfinite(value)
    if type(value) not in (list, dict) or id(value) in seen:
        return False
    seen.add(id(value))
    values = value if type(value) is list else value.values()
    return (type(value) is list or all(type(k) is str for k in value)) and all(lossless_json(x, seen) for x in values)


def atomic_write(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


class Cassette:
    def __init__(self, path, *, write=False, config=None, trust_pickle=False):
        self.path = Path(path).resolve()
        self.blobs = self.path.with_suffix('.blobs')
        self.journal = self.path.with_suffix('.journal.jsonl')
        self.trust_pickle = trust_pickle
        self.lock = threading.RLock()
        self.writable = write
        if write:
            # Exclusive journal creation prevents accidental overwrite and concurrent writers.
            self.path.parent.mkdir(parents=True, exist_ok=True)
            if self.path.exists():
                raise MockerError(f'cassette already exists: {self.path}')
            self.data = {'version': 1, 'complete': False, 'config': config or {}, 'interactions': []}
            with self.journal.open('x') as stream:
                stream.write(json.dumps({'manifest': self.data}) + '\n')
        else:
            self.data = json.loads(self.path.read_text())
            if self.data.get('version') != 1 or not isinstance(self.data.get('interactions'), list):
                raise MockerError('invalid cassette format')
        self.used = set()

    def encode(self, value):
        if lossless_json(value):
            return {'codec': 'json', 'value': json.loads(json.dumps(value, allow_nan=False))}
        try:
            payload = pickle.dumps(value, protocol=5)
        except Exception as exc:
            raise MockerError(f'value is not replayable: {type(value).__name__}: {exc}') from exc
        digest = hashlib.sha256(payload).hexdigest()
        filename = f'{digest}.pkl'
        atomic_write(self.blobs / filename, payload)
        return {'codec': 'pickle', 'path': f'{self.blobs.name}/{filename}', 'sha256': digest}

    def decode(self, reference):
        if reference['codec'] == 'json':
            return json.loads(json.dumps(reference['value']))
        if reference['codec'] != 'pickle':
            raise MockerError('unknown codec')
        if not self.trust_pickle:
            raise MockerError('pickle replay requires --trust-pickle')
        path = (self.path.parent / reference['path']).resolve()
        if not path.is_relative_to(self.blobs.resolve()):
            raise MockerError('blob path escapes cassette directory')
        payload = path.read_bytes()
        if hashlib.sha256(payload).hexdigest() != reference['sha256']:
            raise MockerError('blob digest mismatch')
        return pickle.loads(payload)

    def append(self, interaction):
        if not self.writable:
            raise MockerError('source cassette is read-only')
        with self.lock:
            with self.journal.open('a') as stream:
                stream.write(json.dumps(interaction, allow_nan=False) + '\n')
                stream.flush()
                os.fsync(stream.fileno())
            self.data['interactions'].append(interaction)

    def take(self, key, consume='once'):
        with self.lock:
            for event in sorted(self.data['interactions'], key=lambda e: e['sequence']):
                if event['key'] == key and event['id'] not in self.used:
                    if consume == 'once':
                        descendants = {event['id']}
                        while True:
                            children = {x['id'] for x in self.data['interactions'] if x.get('parent_id') in descendants}
                            if children <= descendants:
                                break
                            descendants |= children
                        self.used |= descendants
                    return event
        return None

    def close(self, complete=True):
        if self.writable:
            with self.lock:
                self.data['complete'] = complete
                self.data['interactions'].sort(key=lambda x: x['sequence'])
                atomic_write(self.path, json.dumps(self.data, indent=2, allow_nan=False).encode())

    @classmethod
    def recover(cls, journal, output):
        lines = Path(journal).read_text().splitlines()
        data = json.loads(lines[0])['manifest']
        data['complete'] = False
        for index, line in enumerate(lines[1:]):
            try:
                data['interactions'].append(json.loads(line))
            except json.JSONDecodeError:
                if index != len(lines) - 2:
                    raise
        expected = Path(journal).with_name(Path(journal).name.removesuffix('.journal.jsonl') + '.json')
        if Path(output).resolve() != expected.resolve():
            raise MockerError('recovery output must use the original cassette path to preserve blob references')
        if Path(output).exists():
            raise MockerError('recovery output exists')
        atomic_write(output, json.dumps(data, indent=2).encode())
