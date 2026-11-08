"""Explicit, detached OpenAPI metadata for custom handlers."""

import json
import re
from copy import deepcopy

from .access import BearerAuth
from .errors import ConfigurationError

_METHODS = frozenset({'get', 'put', 'post', 'delete', 'options', 'head', 'patch', 'trace'})


def _json_data(value):
    pending = [(value, 0)]
    visited = 0
    while pending:
        node, depth = pending.pop()
        visited += 1
        if depth > 32 or visited > 10000:
            raise ConfigurationError('Documentation exceeds the metadata bounds.')
        if isinstance(node, dict):
            if any(type(key) is not str for key in node):
                raise ConfigurationError('Documentation object keys must be strings.')
            pending.extend((item, depth + 1) for item in node.values())
        elif isinstance(node, (list, tuple)):
            pending.extend((item, depth + 1) for item in node)
    try:
        encoded = json.dumps(value, allow_nan=False)
        if len(encoded) > 1024 * 1024:
            raise ValueError
        return json.loads(encoded)
    except (ValueError, TypeError, RecursionError):
        raise ConfigurationError('Documentation must contain bounded JSON values.') from None


class RouteDocs:
    """Metadata only; request schemas never install runtime validation."""

    def __init__(self, *, include=True, operation_id=None, summary=None, description=None,
                 tags=(), parameters=(), request_body=None, responses=None, security=None):
        if (type(include) is not bool or operation_id is not None and
                (type(operation_id) is not str or len(operation_id) > 128 or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_.:-]*', operation_id))):
            raise ConfigurationError('Choose a boolean visibility and bounded operation identifier.')
        if isinstance(tags, str):
            raise ConfigurationError('Tags must be a collection of strings.')
        tags = tuple(tags)
        if (any(value is not None and type(value) is not str for value in (summary, description))
                or any(type(tag) is not str or not tag for tag in tags)):
            raise ConfigurationError('Documentation text and tags must be strings.')
        data = {'responses': responses if responses is not None else {'200': {'description': 'Success'}},
                'parameters': list(parameters), 'tags': list(tags)}
        for name, value in (('summary', summary), ('description', description), ('requestBody', request_body), ('security', security)):
            if value is not None:
                data[name] = value
        data = _json_data(data)
        if not isinstance(data['responses'], dict) or not data['responses']:
            raise ConfigurationError('Document at least one response.')
        for code, response in data['responses'].items():
            if (not re.fullmatch(r'(?:[1-5](?:[0-9]{2}|XX)|default)', code) or not isinstance(response, dict)
                    or type(response.get('description')) is not str or not response['description']
                    or 'headers' in response and not isinstance(response['headers'], dict)
                    or 'content' in response and not isinstance(response['content'], dict)
                    or code in ('204', '205', '304') and 'content' in response):
                raise ConfigurationError('Response metadata requires valid statuses and descriptions.')
        seen = set()
        for parameter in data['parameters']:
            if (not isinstance(parameter, dict) or type(parameter.get('name')) is not str or not parameter['name']
                    or parameter.get('in') not in ('query', 'header', 'cookie')):
                raise ConfigurationError('Custom parameters must be named query, header or cookie parameters.')
            key = parameter['in'], parameter['name']
            if key in seen:
                raise ConfigurationError('Duplicate documentation parameter.')
            seen.add(key)
        if request_body is not None and (not isinstance(data['requestBody'], dict)
                                         or not isinstance(data['requestBody'].get('content'), dict)
                                         or not data['requestBody']['content']):
            raise ConfigurationError('Request body metadata requires explicit media content.')
        if security is not None:
            if not isinstance(data['security'], list):
                raise ConfigurationError('Security requirements must be a list.')
            for requirement in data['security']:
                if (not isinstance(requirement, dict) or any(not isinstance(scopes, list)
                        or any(type(scope) is not str for scope in scopes) for scopes in requirement.values())):
                    raise ConfigurationError('Security requirements map scheme names to scope lists.')
        self.include, self.operation_id, self._data = include, operation_id, data

    def snapshot(self):
        return deepcopy(self)


def security_scheme(name, definition):
    if type(name) is not str or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_.-]{0,127}', name):
        raise ConfigurationError('Security scheme names must be bounded identifiers.')
    result = _json_data(definition)
    if not isinstance(result, dict):
        raise ConfigurationError('Security schemes must be objects.')
    kind = result.get('type')
    if (kind not in ('http', 'apiKey', 'oauth2', 'openIdConnect')
            or kind == 'http' and (type(result.get('scheme')) is not str or not result['scheme'])
            or kind == 'apiKey' and (result.get('in') not in ('query', 'header', 'cookie') or type(result.get('name')) is not str)
            or kind == 'oauth2' and (not isinstance(result.get('flows'), dict) or not result['flows'])
            or kind == 'openIdConnect' and type(result.get('openIdConnectUrl')) is not str):
        raise ConfigurationError('Security scheme metadata is incomplete.')
    return result


def _path(route):
    return re.sub(r'\{([A-Za-z_][A-Za-z0-9_]*)(?::[a-z]+)?\}', r'{\1}', route.path)


def _parameters(route):
    schemas = {'str': {'type': 'string', 'minLength': 1}, 'path': {'type': 'string', 'minLength': 1},
               'int': {'type': 'integer', 'minimum': 0, 'maximum': 10 ** 20 - 1},
               'uuid': {'type': 'string', 'format': 'uuid'}}
    return [{'name': name, 'in': 'path', 'required': True, 'schema': schemas[kind], 'x-path-converter': kind}
            for name, kind in route.parameters]


def extend_document(app, paths, components):
    routes = app._router._routes
    schemes = deepcopy(app._security_schemes)
    auto_bearer = isinstance(app.authenticate, BearerAuth) and (any(route.authenticate for route in routes)
                  or any(mount.record_policy is not None for mount in app._mounts))
    if auto_bearer:
        expected = {'type': 'http', 'scheme': 'bearer'}
        if 'BearerAuth' in schemes and schemes['BearerAuth'] != expected:
            raise ConfigurationError('BearerAuth metadata conflicts with the selected authenticator.')
        schemes['BearerAuth'] = expected
    if schemes:
        components['securitySchemes'] = schemes
    groups, shapes = {}, {}
    for index, route in enumerate(routes):
        if not route.documentation.include:
            continue
        path = _path(route)
        shape = re.sub(r'\{[^}]+\}', '{}', path)
        if shape in shapes and shapes[shape] != path:
            raise ConfigurationError('OpenAPI cannot represent equivalent paths with different parameter names.')
        shapes[shape] = path
        groups.setdefault(path, []).append((index, route))
    for path, group in groups.items():
        if path in paths:
            raise ConfigurationError('Custom documentation collides with an existing path.')
        operations = {}
        if any(route.parameters != group[0][1].parameters for _, route in group):
            raise ConfigurationError('Choose one documented converter contract per OpenAPI path.')
        all_routes = [route for route in routes if _path(route) == path]
        runtime_methods = set().union(*(route.methods for route in all_routes))
        for index, route in group:
            for method in sorted(route.methods):
                key = method.lower()
                if key not in _METHODS:
                    continue
                if key in operations:
                    raise ConfigurationError('OpenAPI cannot represent overlapping converter variants.')
                value = deepcopy(route.documentation._data)
                base = route.documentation.operation_id or f'custom_{index}'
                value['operationId'] = base + ('_' + key if len(route.methods) > 1 or route.documentation.operation_id is None else '')
                value['parameters'] = _parameters(route) + value['parameters']
                value['x-access-policy'] = 'authenticated-callback' if route.authenticate else 'callback' if route.authorize else 'public'
                value['x-runtime-validation'] = False
                codes = {400, 405, 500}
                if route.authorize is not None:
                    codes.add(403)
                if route.authenticate:
                    codes.add(401)
                    value['x-authentication'] = 'caller-owned'
                    if 'security' not in value and isinstance(app.authenticate, BearerAuth):
                        value['security'] = [{'BearerAuth': []}]
                if 'requestBody' in value:
                    codes.update({413, 415})
                    value['x-max-body-bytes'] = app.max_body_size
                for code in codes:
                    value['responses'].setdefault(str(code), {'description': 'Framework or handler error', 'content': {
                        'application/json': {'schema': {'$ref': '#/components/schemas/Error'}}}})
                if route.authorize is not None:
                    for response in value['responses'].values():
                        response.setdefault('headers', {})['Cache-Control'] = {'schema': {'const': 'no-store'}}
                if route.authenticate:
                    value['responses']['401'].setdefault('headers', {})['WWW-Authenticate'] = {'schema': {'type': 'string'}}
                operations[key] = value
        if 'get' in operations and 'HEAD' not in runtime_methods:
            head = deepcopy(operations['get'])
            head['operationId'] += '_head'
            for response in head['responses'].values():
                response.pop('content', None)
            operations['head'] = head
        if 'OPTIONS' not in runtime_methods:
            allow = runtime_methods | {'OPTIONS'} | ({'HEAD'} if 'GET' in runtime_methods else set())
            headers = {'Allow': {'schema': {'const': ', '.join(sorted(allow))}}}
            if any(route.authorize is not None for route in all_routes):
                headers['Cache-Control'] = {'schema': {'const': 'no-store'}}
            operations['options'] = {'operationId': f'custom_{group[0][0]}_options', 'parameters': _parameters(group[0][1]), 'responses': {
                '204': {'description': 'Allowed methods', 'headers': headers}}}
        unknown = sorted(runtime_methods - {method.upper() for method in _METHODS})
        if unknown:
            operations['x-http-methods'] = unknown
        if operations:
            paths[path] = operations
    identifiers = set()
    for item in paths.values():
        for key, operation in item.items():
            if key not in _METHODS:
                continue
            identity = operation.get('operationId')
            if identity in identifiers:
                raise ConfigurationError('OpenAPI operation identifiers must be unique.')
            if identity is not None:
                identifiers.add(identity)
            for requirement in operation.get('security', []):
                if any(name not in schemes for name in requirement):
                    raise ConfigurationError('Documented security requires a registered scheme.')
