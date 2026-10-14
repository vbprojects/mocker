import contextvars
import hashlib
import importlib
import json
import threading
import time
import uuid
from .errors import Blocked, MockerError, ReplayMiss
from .storage import lossless_json

_parent = contextvars.ContextVar('mocker_parent', default=None)


def fingerprint(event):
    # Preserve insertion order: dictionaries and keyword order can be semantically significant.
    payload = {'version': 1, 'kind': event['kind'], 'target': event['target'], 'input': event['input']}
    if not lossless_json(payload):
        raise MockerError('inputs need a lossless JSON matching representation; configure a JSON receiver key or adapter')
    return hashlib.sha256(json.dumps(payload, separators=(',', ':'), allow_nan=False).encode()).hexdigest()


class Engine:
    def __init__(self, config, mode, source=None, destination=None):
        self.config, self.mode, self.source, self.destination = config, mode, source, destination
        self.lock = threading.Lock()
        self.sequence = 0

    def prepare(self, event):
        action, rule = self.config.resolve(event, self.mode)
        if action['type'] == 'block':
            raise Blocked(f'blocked {event["target"]} by {rule}')
        if action['type'] == 'live':
            return None, action, None
        if action['type'] == 'mock':
            return None, action, json.loads(json.dumps(action['outcome']))
        matching_error = None
        try:
            key = fingerprint(event)
            snapshot = json.loads(json.dumps(event))
        except MockerError as exc:
            if action['type'] != 'record' or not self.destination:
                raise
            matching_error = str(exc)
            key = 'unmatchable:' + uuid.uuid4().hex
            try:
                stored_input = self.destination.encode(event['input'])
            except MockerError as storage_error:
                stored_input = {'codec': 'unsupported', 'error': str(storage_error)}
            snapshot = {**event, 'input': stored_input}
        with self.lock:
            self.sequence += 1
            sequence = self.sequence
        record = {**snapshot, 'id': uuid.uuid4().hex, 'key': key, 'sequence': sequence,
                  'parent_id': _parent.get(), 'source': 'live', 'started_ns': time.time_ns()}
        if matching_error:
            record['matching_error'] = matching_error
        if action['type'] == 'replay':
            hit = self.source.take(key, self.config.consume) if self.source else None
            if hit:
                if hit.get('replayability') == 'unsupported':
                    raise MockerError(hit.get('error', 'recording is not replayable'))
                outcome = hit['outcome']
                if event['kind'] == 'python':
                    outcome = {k: self.source.decode(v) if k == 'return' else v for k, v in outcome.items()}
                return None, action, outcome
            miss = action.get('on_miss', self.config.on_miss)
            if miss == 'error':
                raise ReplayMiss(f'no remaining recording for {event["target"]} (key {key})')
            action = {'type': miss}
        if action['type'] == 'record' and not self.destination:
            raise MockerError('recording requires a separate writable destination cassette')
        return record, action, None

    def finish(self, record, action, outcome):
        if action['type'] == 'record':
            record['finished_ns'] = time.time_ns()
            try:
                if record['kind'] == 'python' and 'return' in outcome:
                    outcome = {'return': self.destination.encode(outcome['return'])}
                record.update(outcome=outcome, replayability='supported')
                if record.get('matching_error'):
                    record.update(replayability='unsupported', error=record['matching_error'])
            except MockerError as exc:
                record.update(outcome={}, replayability='unsupported', error=str(exc))
            self.destination.append(record)

    @staticmethod
    def deliver(outcome):
        if 'raise' in outcome:
            spec = outcome['raise']
            module, name = spec['type'].split(':', 1)
            # Exception construction is restricted to builtins; custom exceptions need a codec.
            if module != 'builtins' or '.' in name:
                raise MockerError('custom exception replay is unsupported')
            cls = getattr(importlib.import_module(module), name)
            if not isinstance(cls, type) or not issubclass(cls, Exception):
                raise MockerError('invalid exception type')
            raise cls(*spec['args'])
        return outcome['return']

    @staticmethod
    def exception(exc):
        args = list(exc.args)
        if type(exc).__module__ != 'builtins' or not lossless_json(args):
            raise MockerError('custom/non-JSON exception is not replayable')
        return {'raise': {'type': f'builtins:{type(exc).__name__}', 'args': args}}
