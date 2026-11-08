"""Ordered, explicit handler registrations separate from CRUD resources."""

import re
from dataclasses import dataclass
from uuid import UUID
from urllib.parse import quote

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
    name: str | None

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


def make_route(path, handler, *, methods=('GET',), public=False, authorize=None, name=None):
    if (type(path) is not str or not path.startswith('/') or path.startswith('//')
            or not callable(handler) or isinstance(methods, str)):
        raise ConfigurationError('A route requires a local path, handler and method collection.')
    selected = frozenset(methods)
    if not selected or any(type(method) is not str or not _METHOD.fullmatch(method) for method in selected):
        raise ConfigurationError('Route methods must be uppercase HTTP tokens.')
    if type(public) is not bool or public and authorize is not None or not public and not callable(authorize):
        raise ConfigurationError('Choose public=True or an authorization callback.')
    if name is not None and (type(name) is not str or len(name) > 128
                             or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_.:-]*', name)):
        raise ConfigurationError('Route names must be bounded identifiers.')
    route_name = name
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
                 tuple(parameters), '/' + '/'.join(shapes), ('/' + '/'.join(static)).rstrip('/'), route_name)


def _prefix(prefix):
    if type(prefix) is not str or prefix and not re.fullmatch(r'/[A-Za-z0-9_~-]+(?:/[A-Za-z0-9_~-]+)*', prefix):
        raise ConfigurationError('Router prefixes must contain static segments without a trailing slash.')
    return prefix


def prefixed_routes(router, prefix='', name_prefix=''):
    if not isinstance(router, Router) or type(name_prefix) is not str:
        raise ConfigurationError('Include a Router with an optional text name prefix.')
    _prefix(prefix)
    return [make_route(prefix + route.path, route.handler, methods=route.methods,
                       public=route.authorize is None, authorize=route.authorize,
                       name=name_prefix + route.name if route.name is not None else None)
            for route in router._routes]


class Router:
    """Ordered handler configuration; App owns serving and resource integration."""

    def __init__(self, *, prefix=''):
        self.prefix = _prefix(prefix)
        self._routes = []

    def _add(self, route):
        self._extend([route])

    def _extend(self, routes):
        combined = list(self._routes)
        for route in routes:
            if any(route.shape == existing.shape and route.methods & existing.methods
                   or route.name is not None and route.name == existing.name for existing in combined):
                raise ConfigurationError('Duplicate route pattern/method or name.')
            combined.append(route)
        self._routes = combined

    def route(self, path, *, methods=('GET',), public=False, authorize=None, name=None):
        def register(handler):
            if type(path) is not str or not path.startswith('/'):
                raise ConfigurationError('Route paths must start with a slash.')
            self._add(make_route(self.prefix + path, handler, methods=methods, public=public,
                                 authorize=authorize, name=name))
            return handler
        return register

    def include_router(self, router, *, prefix='', name_prefix=''):
        self._extend(prefixed_routes(router, self.prefix + _prefix(prefix), name_prefix))

    def url_path_for(self, name, /, **params):
        return self._url_path_for(name, params, '')

    def _url_path_for(self, name, params, root_path):
        route = next((route for route in self._routes if route.name == name and name is not None), None)
        if route is None or set(params) != {key for key, _ in route.parameters}:
            raise ConfigurationError('Unknown route name or mismatched URL parameters.')
        path = route.path
        for key, kind in route.parameters:
            value = params[key]
            if kind == 'int':
                valid = type(value) is int and 0 <= value < 10 ** 20
            elif kind == 'uuid':
                valid = type(value) is UUID
            else:
                segments = value.split('/') if type(value) is str and kind == 'path' else [value]
                valid = all(type(segment) is str and segment not in ('', '.', '..')
                            and not any(c in '/\\' or ord(c) < 32 or ord(c) == 127 for c in segment)
                            for segment in segments)
            if not valid:
                raise ConfigurationError('Invalid URL parameter.')
            encoded = quote(str(value), safe='/' if kind == 'path' else '')
            path = re.sub(r'\{' + re.escape(key) + r'(?::[a-z]+)?\}', lambda match: encoded, path)
        if (type(root_path) is not str or root_path and not root_path.startswith('/')
                or root_path.startswith('//') or any(c in '\\?#' or ord(c) < 32 for c in root_path)):
            raise ConfigurationError('Invalid local URL root path.')
        return quote(root_path.rstrip('/'), safe='/') + path
