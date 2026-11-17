"""Opt-in finite security profiles for ordinary ASGI applications."""

import hmac
import inspect
import ipaddress
import re
from urllib.parse import urlsplit

from .errors import ConfigurationError
from .requests import HTTPRequest
from .http_protocol import _HTTPError
from .responses import JSONResponse, Response, _headers

_TOKEN = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+")


def _values(scope, name):
    return [value.decode('latin-1') for key, value in scope.get('headers', ()) if key.lower() == name]


def _authority(value):
    if (type(value) is not str or not 1 <= len(value) <= 512
            or value.endswith(':') or any(ord(c) < 33 or ord(c) > 126 or c in '/\\?#@%' for c in value)):
        raise ValueError
    parsed = urlsplit('http://' + value)
    host, port = parsed.hostname, parsed.port
    if not host or parsed.netloc != value or port is not None and not 1 <= port <= 65535:
        raise ValueError
    if ':' in host:
        host = ipaddress.IPv6Address(host).compressed
    elif (len(host) > 253 or not all(re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', label)
                                    for label in host.split('.'))):
        raise ValueError
    return host, port


def _origin(value):
    if (type(value) is not str or not 1 <= len(value) <= 1024
            or any(ord(c) < 33 or ord(c) > 126 for c in value)):
        raise ValueError
    parsed = urlsplit(value)
    if (parsed.scheme not in ('http', 'https') or parsed.path or parsed.query or parsed.fragment
            or value.lower() != (parsed.scheme + '://' + parsed.netloc).lower()):
        raise ValueError
    host, port = _authority(parsed.netloc)
    host = '[' + host + ']' if ':' in host else host
    return parsed.scheme + '://' + host + (':' + str(port) if port and port != (443 if parsed.scheme == 'https' else 80) else '')


def _origins(values, *, wildcard=False):
    if isinstance(values, str):
        raise ConfigurationError('Select a collection of explicit origins.')
    try:
        selected = frozenset('*' if wildcard and value == '*' else _origin(value) for value in values)
    except (ValueError, TypeError):
        raise ConfigurationError('Origins must be explicit HTTP(S) origins without paths or credentials.') from None
    if not selected:
        raise ConfigurationError('Select at least one allowed origin.')
    return selected


def _metadata_request(scope, receive):
    request = HTTPRequest(scope, receive)
    request._failure = ConfigurationError('Security decision callbacks cannot consume request bodies.')
    return request


async def _deny(scope, receive, send, status, code, *, headers=()):
    if scope['type'] == 'websocket':
        await send({'type': 'websocket.close', 'code': 1008})
    else:
        response = JSONResponse({'error': {'code': code, 'message': 'Request rejected.', 'details': []}},
                                status=status, headers=[('cache-control', 'no-store'), *headers])
        await response(scope, receive, send)


def _vary(headers, names):
    values = [value.decode('latin-1') for key, value in headers if key.lower() == b'vary']
    tokens = [token.strip() for value in values for token in value.split(',') if token.strip()]
    lowered = {token.lower() for token in tokens}
    if '*' not in lowered:
        tokens.extend(name for name in names if name.lower() not in lowered)
    return [(key, value) for key, value in headers if key.lower() != b'vary'] + [(b'vary', ', '.join(tokens).encode('latin-1'))]


async def _decorate(app, scope, receive, send, transform):
    # Native App also applies these filters to its outer exception boundary.
    state = scope.get('expose_api.request_state')
    if state is not None:
        state.header_filters.append(transform)
        await app(scope, receive, send)
    else:
        async def decorated(event):
            if event.get('type') == 'http.response.start':
                event = {**event, 'headers': transform(list(event.get('headers', ())))}
            await send(event)
        await app(scope, receive, decorated)


class TrustedHostMiddleware:
    def __init__(self, app, *, allowed_hosts):
        if isinstance(allowed_hosts, str):
            raise ConfigurationError('Select a collection of trusted hosts.')
        self.app, self.hosts = app, []
        try:
            for value in allowed_hosts:
                wildcard = value.startswith('*.')
                host, port = _authority(value[2:] if wildcard else value)
                if port is not None or wildcard and ':' in host:
                    raise ValueError
                self.hosts.append((host, wildcard))
        except (ValueError, TypeError, AttributeError):
            raise ConfigurationError('Trusted hosts require hostnames/IPs or whole subdomain wildcards, without ports.') from None
        if not self.hosts:
            raise ConfigurationError('Select at least one trusted host.')

    async def __call__(self, scope, receive, send):
        if scope['type'] not in ('http', 'websocket'):
            await self.app(scope, receive, send)
            return
        values = _values(scope, b'host')
        try:
            if len(values) != 1:
                raise ValueError
            host, _ = _authority(values[0])
            allowed = any(host.endswith('.' + selected) if wildcard else host == selected
                          for selected, wildcard in self.hosts)
        except ValueError:
            allowed = False
        if not allowed:
            await _deny(scope, receive, send, 400, 'host')
            return
        await self.app(scope, receive, send)


class ProxyHeadersMiddleware:
    """Trust X-Forwarded-For/Proto only from selected immediate proxy peers."""

    def __init__(self, app, *, trusted_proxies):
        if isinstance(trusted_proxies, str):
            raise ConfigurationError('Select a collection of trusted proxy networks.')
        try:
            self.trusted = tuple(ipaddress.ip_network(value) for value in trusted_proxies)
        except (ValueError, TypeError):
            raise ConfigurationError('Trusted proxies must be explicit IP addresses or CIDR networks.') from None
        if not self.trusted:
            raise ConfigurationError('Select at least one trusted proxy network.')
        self.app = app

    def _trusted(self, address):
        return any(address in network for network in self.trusted)

    async def __call__(self, scope, receive, send):
        if scope['type'] not in ('http', 'websocket'):
            await self.app(scope, receive, send)
            return
        child = dict(scope)
        child['headers'] = [(key, value) for key, value in scope.get('headers', ())
                            if key.lower() not in (b'forwarded', b'x-real-ip') and not key.lower().startswith(b'x-forwarded-')]
        try:
            peer = ipaddress.ip_address(scope['client'][0])
        except (ValueError, KeyError, TypeError, IndexError):
            peer = None
        if peer is not None and self._trusted(peer):
            try:
                forwarded, proto = _values(scope, b'x-forwarded-for'), _values(scope, b'x-forwarded-proto')
                if len(forwarded) > 1 or len(proto) > 1:
                    raise ValueError
                if forwarded:
                    if len(forwarded[0]) > 8192:
                        raise ValueError
                    chain = forwarded[0].split(',')
                    if not 1 <= len(chain) <= 32:
                        raise ValueError
                    addresses = [ipaddress.ip_address(value.strip()) for value in chain]
                    for address in reversed(addresses):
                        if not self._trusted(address):
                            break
                    child['client'] = (str(address), 0)
                if proto:
                    if proto[0] not in ('http', 'https'):
                        raise ValueError
                    child['scheme'] = ('wss' if proto[0] == 'https' else 'ws') if scope['type'] == 'websocket' else proto[0]
            except ValueError:
                await _deny(scope, receive, send, 400, 'proxy')
                return
        await self.app(child, receive, send)


class SecurityHeadersMiddleware:
    def __init__(self, app, *, headers=(), hsts_max_age=None):
        selected = dict(_headers({'x-content-type-options': 'nosniff', 'x-frame-options': 'DENY',
                                  'referrer-policy': 'strict-origin-when-cross-origin',
                                  'content-security-policy': "default-src 'self'; object-src 'none'; base-uri 'self'; frame-ancestors 'none'"}))
        selected.update(_headers(headers))
        if (set(selected) & {b'content-length', b'transfer-encoding', b'set-cookie'}
                or hsts_max_age is not None and (type(hsts_max_age) is not int or not 0 <= hsts_max_age <= 63072000)):
            raise ConfigurationError('Security headers cannot own response framing/cookies; select a bounded HSTS age.')
        self.app, self.headers, self.hsts_max_age = app, tuple(selected.items()), hsts_max_age

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            await self.app(scope, receive, send)
            return
        selected = dict(self.headers)
        if self.hsts_max_age is not None and scope.get('scheme') == 'https':
            selected[b'strict-transport-security'] = ('max-age=' + str(self.hsts_max_age)).encode('ascii')
        def transform(headers):
            return [(key, value) for key, value in headers if key.lower() not in selected] + list(selected.items())
        await _decorate(self.app, scope, receive, send, transform)


class CORSMiddleware:
    def __init__(self, app, *, origins, methods=('GET',), headers=(), expose_headers=(), credentials=False, max_age=600):
        self.origins = _origins(origins, wildcard=True)
        if any(isinstance(value, str) for value in (methods, headers, expose_headers)):
            raise ConfigurationError('CORS methods/headers must be explicit collections.')
        try:
            methods, headers, expose_headers = tuple(methods), tuple(headers), tuple(expose_headers)
            if any(type(name) is not str for name in (*headers, *expose_headers)):
                raise ValueError
            self.methods, self.headers, self.exposed = frozenset(methods), frozenset(name.lower() for name in headers), expose_headers
        except (TypeError, ValueError):
            raise ConfigurationError('CORS methods/headers must be explicit string collections.') from None
        if (not self.methods or any(type(method) is not str or not _TOKEN.fullmatch(method) or method != method.upper() or method == '*' for method in self.methods)
                or any(type(name) is not str or not _TOKEN.fullmatch(name) or name == '*' for name in (*self.headers, *self.exposed))
                or type(credentials) is not bool or credentials and '*' in self.origins
                or type(max_age) is not int or not 0 <= max_age <= 86400):
            raise ConfigurationError('Select explicit CORS methods/headers and a bounded cache age; credentialed wildcard origins are forbidden.')
        self.app, self.credentials, self.max_age = app, credentials, max_age

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http' or not _values(scope, b'origin'):
            await self.app(scope, receive, send)
            return
        origins = _values(scope, b'origin')
        try:
            if len(origins) != 1 or _origin(origins[0]) not in self.origins and '*' not in self.origins:
                raise ValueError
            requested = _values(scope, b'access-control-request-method')
            preflight = scope['method'] == 'OPTIONS' and bool(requested)
            method = requested[0] if preflight and len(requested) == 1 else scope['method']
            if len(requested) > 1 or method not in self.methods:
                raise ValueError
            selected = [(b'access-control-allow-origin', b'*' if '*' in self.origins else origins[0].encode('ascii'))]
            if self.credentials:
                selected.append((b'access-control-allow-credentials', b'true'))
            if preflight:
                raw = ','.join(_values(scope, b'access-control-request-headers'))
                names = [name.strip().lower() for name in raw.split(',')] if raw else []
                if len(raw) > 8192 or len(names) > 128 or any(not _TOKEN.fullmatch(name) or name not in self.headers for name in names):
                    raise ValueError
                selected += [(b'access-control-allow-methods', ', '.join(sorted(self.methods)).encode('ascii')),
                             (b'access-control-max-age', str(self.max_age).encode('ascii'))]
                if names:
                    selected.append((b'access-control-allow-headers', ', '.join(sorted(set(names))).encode('ascii')))
            elif self.exposed:
                selected.append((b'access-control-expose-headers', ', '.join(self.exposed).encode('ascii')))
        except ValueError:
            await _deny(scope, receive, send, 403, 'cors', headers=[('vary', 'Origin, Access-Control-Request-Method, Access-Control-Request-Headers')])
            return
        def transform(headers):
            result = [(key, value) for key, value in headers if not key.lower().startswith(b'access-control-')]
            return _vary([*result, *selected], ('Origin', 'Access-Control-Request-Method', 'Access-Control-Request-Headers') if preflight else ('Origin',))
        if preflight:
            await Response(status=204, headers=transform([]))(scope, receive, send)
        else:
            await _decorate(self.app, scope, receive, send, transform)


class CSRFMiddleware:
    """Check configured origins and caller-owned session tokens before body reads."""

    def __init__(self, app, *, origins, token, cookie_name='session', header_name='x-csrf-token', always=False):
        self.origins = _origins(origins)
        if (not callable(token) or type(cookie_name) is not str or not _TOKEN.fullmatch(cookie_name)
                or type(header_name) is not str or not _TOKEN.fullmatch(header_name) or type(always) is not bool):
            raise ConfigurationError('Select a session cookie, token header and caller-owned expected-token provider.')
        self.app, self.token, self.cookie_name, self.header_name, self.always = app, token, cookie_name, header_name.lower(), always

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http' or scope['method'] in ('GET', 'HEAD', 'OPTIONS'):
            await self.app(scope, receive, send)
            return
        request = _metadata_request(scope, receive)
        try:
            raw_cookies = _values(scope, b'cookie')
            if len(raw_cookies) > 1 or raw_cookies and len(raw_cookies[0]) > 8192:
                raise ValueError
            names = []
            for part in raw_cookies[0].split(';') if raw_cookies else ():
                name, separator, _ = part.strip().partition('=')
                if not separator or not _TOKEN.fullmatch(name) or name in names:
                    raise ValueError
                names.append(name)
            if set(request.cookies) != set(names):
                raise ValueError
            if not self.always and self.cookie_name not in names:
                await self.app(scope, receive, send)
                return
            origins = _values(scope, b'origin')
            if origins:
                if len(origins) != 1:
                    raise ValueError
                origin = _origin(origins[0])
            else:
                referers = _values(scope, b'referer')
                if len(referers) != 1 or len(referers[0]) > 8192 or any(ord(c) < 33 or ord(c) > 126 for c in referers[0]):
                    raise ValueError
                parsed = urlsplit(referers[0])
                origin = _origin(parsed.scheme + '://' + parsed.netloc)
            submitted = request.header(self.header_name)
            if origin not in self.origins or submitted is None or not 16 <= len(submitted) <= 4096:
                raise ValueError
            submitted = submitted.encode('ascii')
        except (ValueError, UnicodeError, _HTTPError):
            await _deny(scope, receive, send, 403, 'csrf')
            return
        expected = self.token(request)
        if inspect.isawaitable(expected):
            expected = await expected
        if expected is not None:
            if type(expected) is str:
                expected = expected.encode('ascii')
            if (type(expected) is not bytes or not 16 <= len(expected) <= 4096
                    or any(byte < 33 or byte > 126 for byte in expected)):
                raise ConfigurationError('Expected CSRF tokens must be bounded ASCII strings or bytes.')
        if expected is None or not hmac.compare_digest(expected, submitted):
            await _deny(scope, receive, send, 403, 'csrf')
            return
        await self.app(scope, receive, send)


class RateLimitMiddleware:
    """A caller-owned shared decision: None permits; integer retry seconds denies."""

    def __init__(self, app, *, decide):
        if not callable(decide):
            raise ConfigurationError('Select a caller-owned rate-limit decision callback.')
        self.app, self.decide = app, decide

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            await self.app(scope, receive, send)
            return
        decision = self.decide(_metadata_request(scope, receive))
        if inspect.isawaitable(decision):
            decision = await decision
        if decision is not None:
            if type(decision) is not int or not 0 <= decision <= 86400:
                raise ConfigurationError('Rate limits return None or bounded integer retry seconds.')
            await _deny(scope, receive, send, 429, 'rate_limit', headers=[('retry-after', str(decision))])
            return
        await self.app(scope, receive, send)
