"""Ordered, explicit handler registrations separate from CRUD resources."""

import re
from dataclasses import dataclass
from uuid import UUID

from .errors import ConfigurationError

_CONVERTERS = {
    'str': (r'[^/]+', str),
    'int': (r'[0-9]{1,20}', int),
    'uuid': (r'[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}', UUID),
    'path': (r'.+', str),
}
_PARAMETER = re.compile(r'\{([A-Za-z_][A-Za-z0-9_]*)(?::(str|int|uuid|path))?\}')
_METHOD = re.compile(r"[!#$%&'*+.^_`|~0-9A-Z-]+")


@dataclass(frozen=True, slots=True)
class Route:
    path: str
    methods: frozenset
    handler: object
    authorize: object
    pattern: object
    parameters: tuple
    shape: str
    static_prefix: str

    def match(self, path):
        matched = self.pattern.fullmatch(path)
        if matched is None:
            return None
        return {name: _CONVERTERS[kind][1](matched[name]) for name, kind in self.parameters}

    def overlaps(self, prefix):
        if not self.parameters:
            return self.path == prefix or self.path.startswith(prefix + '/')
        static = self.static_prefix
        return (not static or static == prefix or static.startswith(prefix + '/')
                or prefix.startswith(static + '/'))


def make_route(path, handler, *, methods=('GET',), public=False, authorize=None):
    if (type(path) is not str or not path.startswith('/') or path.startswith('//')
            or not callable(handler) or isinstance(methods, str)):
        raise ConfigurationError('A route requires a local path, handler and method collection.')
    selected = frozenset(methods)
    if not selected or any(type(method) is not str or not _METHOD.fullmatch(method) for method in selected):
        raise ConfigurationError('Route methods must be uppercase HTTP tokens.')
    if type(public) is not bool or public and authorize is not None or not public and not callable(authorize):
        raise ConfigurationError('Choose public=True or an authorization callback.')
    expressions, parameters, shapes, static = [], [], [], []
    dynamic = False
    for index, segment in enumerate(path.split('/')[1:]):
        matched = _PARAMETER.fullmatch(segment)
        if matched:
            name, kind = matched[1], matched[2] or 'str'
            if name in dict(parameters) or kind == 'path' and index != len(path.split('/')) - 2:
                raise ConfigurationError('Route parameters must be unique; path must be last.')
            expressions.append('(?P<' + name + '>' + _CONVERTERS[kind][0] + ')')
            parameters.append((name, kind))
            shapes.append('{' + kind + '}')
            dynamic = True
        else:
            if (not re.fullmatch(r'[A-Za-z0-9_.~-]*', segment) or segment in ('.', '..')
                    or not segment and path != '/' and index != len(path.split('/')) - 2):
                raise ConfigurationError('Invalid static route segment.')
            expressions.append(re.escape(segment))
            shapes.append(segment)
            if not dynamic:
                static.append(segment)
    return Route(path, selected, handler, authorize, re.compile('/' + '/'.join(expressions)),
                 tuple(parameters), '/' + '/'.join(shapes), ('/' + '/'.join(static)).rstrip('/'))


class Router:
    """Ordered handler configuration; App owns serving and resource integration."""

    def __init__(self):
        self._routes = []

    def _add(self, route):
        if any(route.shape == existing.shape and route.methods & existing.methods for existing in self._routes):
            raise ConfigurationError('Duplicate route pattern and method.')
        self._routes.append(route)

    def route(self, path, *, methods=('GET',), public=False, authorize=None):
        def register(handler):
            self._add(make_route(path, handler, methods=methods, public=public, authorize=authorize))
            return handler
        return register
