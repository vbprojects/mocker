"""Source instrumentation installs wrappers before module-level calls execute."""
import ast
import functools
import importlib.abc
import importlib.machinery
import inspect
import sys
from .engine import _parent
from .errors import MockerError


class Instrumentor:
    def __init__(self, engine):
        self.engine = engine
        self.inventory = set()

    def wrap(self, target):
        def decorate(original):
            if inspect.isgeneratorfunction(original) or inspect.isasyncgenfunction(original):
                raise MockerError(f'generator capture unsupported: {target}')
            signature = inspect.signature(original)
            module, name = target.split(':', 1)

            def prepare(args, kwargs):
                bound = signature.bind(*args, **kwargs)
                bound.apply_defaults()
                event = {'kind': 'python', 'target': target, 'module': module,
                         'name': name.rsplit('.', 1)[-1], 'input': {'arguments': dict(bound.arguments)}}
                if not self.engine.config.selected(event, self.engine.mode):
                    return None
                return self.engine.prepare(event)

            def failure(record, action, exc):
                try:
                    outcome = self.engine.exception(exc)
                except MockerError as error:
                    if action['type'] == 'record':
                        self.engine.destination.append({**record, 'outcome': {}, 'replayability': 'unsupported', 'error': str(error)})
                else:
                    self.engine.finish(record, action, outcome)

            if inspect.iscoroutinefunction(original):
                @functools.wraps(original)
                async def wrapper(*args, **kwargs):
                    prepared = prepare(args, kwargs)
                    if prepared is None:
                        return await original(*args, **kwargs)
                    record, action, outcome = prepared
                    if outcome is not None:
                        return self.engine.deliver(outcome)
                    token = _parent.set(record['id']) if record else None
                    try:
                        result = await original(*args, **kwargs)
                    except Exception as exc:
                        failure(record, action, exc)
                        raise
                    else:
                        self.engine.finish(record, action, {'return': result})
                        return result
                    finally:
                        if token is not None:
                            _parent.reset(token)
            else:
                @functools.wraps(original)
                def wrapper(*args, **kwargs):
                    prepared = prepare(args, kwargs)
                    if prepared is None:
                        return original(*args, **kwargs)
                    record, action, outcome = prepared
                    if outcome is not None:
                        return self.engine.deliver(outcome)
                    token = _parent.set(record['id']) if record else None
                    try:
                        result = original(*args, **kwargs)
                    except Exception as exc:
                        failure(record, action, exc)
                        raise
                    else:
                        self.engine.finish(record, action, {'return': result})
                        return result
                    finally:
                        if token is not None:
                            _parent.reset(token)
            self.inventory.add(target)
            return wrapper
        return decorate

    def compile(self, source, filename, module):
        instrumentor = self

        class Transformer(ast.NodeTransformer):
            def __init__(self):
                self.stack = []

            def visit_ClassDef(self, node):
                self.stack.append(node.name)
                self.generic_visit(node)
                self.stack.pop()
                return node

            def function(self, node):
                qualified = '.'.join(self.stack + [node.name])
                target = f'{module}:{qualified}'
                event = {'kind': 'python', 'target': target, 'module': module, 'name': node.name}
                self.stack.extend([node.name, '<locals>'])
                self.generic_visit(node)
                self.stack[-2:] = []
                if instrumentor.engine.config.selected(event, instrumentor.engine.mode, definition=True):
                    node.decorator_list.append(ast.Call(func=ast.Name(id='__mocker_wrap__', ctx=ast.Load()),
                                                        args=[ast.Constant(target)], keywords=[]))
                return node

            visit_FunctionDef = function
            visit_AsyncFunctionDef = function

        tree = Transformer().visit(ast.parse(source, filename))
        return compile(ast.fix_missing_locations(tree), filename, 'exec')

    def execute(self, source, filename, module, namespace):
        namespace['__mocker_wrap__'] = self.wrap
        exec(self.compile(source, filename, module), namespace)


class SourceLoader(importlib.machinery.SourceFileLoader):
    def __init__(self, name, path, instrumentor):
        super().__init__(name, path)
        self.instrumentor = instrumentor

    def exec_module(self, module):
        source = self.get_source(module.__name__)
        self.instrumentor.execute(source, self.path, module.__name__, module.__dict__)


class Finder(importlib.abc.MetaPathFinder):
    def __init__(self, instrumentor, scope):
        self.instrumentor, self.scope = instrumentor, scope

    def find_spec(self, fullname, path=None, target=None):
        if fullname.startswith('mocker') or not any(fullname == s or fullname.startswith(s + '.') for s in self.scope):
            return None
        spec = importlib.machinery.PathFinder.find_spec(fullname, path)
        if spec and isinstance(spec.loader, importlib.machinery.SourceFileLoader):
            spec.loader = SourceLoader(fullname, spec.origin, self.instrumentor)
        return spec

    def install(self):
        sys.meta_path.insert(0, self)
