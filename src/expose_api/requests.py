"""Bounded request readers for explicitly registered HTTP handlers."""

import json
import re
from collections.abc import Mapping
from http.cookies import CookieError, SimpleCookie
from urllib.parse import parse_qsl

from .errors import ConfigurationError
from .http_protocol import ProtocolError, _Disconnected, _HTTPError


class MultiDict(Mapping):
    """Immutable ordered pairs; mapping reads select the last repeated value."""

    def __init__(self, pairs=()):
        self._pairs = tuple(pairs)
        self._values = dict(self._pairs)

    def __getitem__(self, key):
        return self._values[key]

    def __iter__(self):
        return iter(self._values)

    def __len__(self):
        return len(self._values)

    def getlist(self, key):
        return [value for name, value in self._pairs if name == key]

    def multi_items(self):
        return list(self._pairs)


def _fields(raw, *, limit, max_fields, code):
    try:
        if len(raw) > limit or re.search(rb'%(?![0-9a-fA-F]{2})', raw):
            raise ValueError
        pairs = parse_qsl(raw.decode('ascii'), keep_blank_values=True, encoding='utf-8',
                          errors='strict', max_num_fields=max_fields)
        return MultiDict(pairs)
    except (ValueError, UnicodeError):
        raise _HTTPError(400, code, 'Invalid encoded fields.') from None


class HTTPRequest:
    """A request-scoped reader. Failed/interrupted body consumption cannot restart."""

    def __init__(self, scope, receive, *, max_body_size=1024 * 1024, path_params=None, app=None):
        if type(max_body_size) is not int or max_body_size < 1:
            raise ConfigurationError('max_body_size must be a positive integer.')
        self.scope = dict(scope)
        self.scope['headers'] = tuple(scope.get('headers', ()))
        self._receive, self.max_body_size, self.app = receive, max_body_size, app
        self.path_params = dict(path_params or {})
        self.state = self.scope.setdefault('state', {}).copy()
        self.scope['state'] = self.state
        self.principal = None
        self._body = self._failure = None
        self._reading = False

    @property
    def method(self):
        return self.scope['method']

    @property
    def path(self):
        return self.scope['path']

    @property
    def headers(self):
        return MultiDict((key.decode('ascii').lower(), value.decode('latin-1'))
                         for key, value in self.scope['headers'])

    def header(self, name):
        values = self.headers.getlist(name.lower())
        return values[0] if len(values) == 1 else None

    def url_for(self, name, /, **params):
        """Return a local encoded path; Host headers never supply an authority."""
        if self.app is None:
            raise ConfigurationError('URL reversal requires an application-bound request.')
        return self.app._url_path_for(name, params, self.scope.get('root_path', ''))

    @property
    def query_params(self):
        return _fields(self.scope.get('query_string', b''), limit=8192, max_fields=128, code='query')

    @property
    def cookies(self):
        raw = self.header('cookie')
        if raw is None:
            return {}
        if len(raw) > 8192:
            raise _HTTPError(400, 'cookie', 'Cookie header exceeds the size limit.')
        try:
            cookie = SimpleCookie()
            cookie.load(raw)
            return {name: morsel.value for name, morsel in cookie.items()}
        except CookieError:
            raise _HTTPError(400, 'cookie', 'Invalid cookie header.') from None

    def _media_type(self, expected):
        raw = self.header('content-type')
        if raw is None:
            raise _HTTPError(415, 'media_type', 'A supported Content-Type is required.')
        media, *parameters = raw.lower().split(';')
        media = media.strip()
        if not (media == expected or expected == 'application/json'
                and re.fullmatch(r'application/[a-z0-9!#$&^_.+-]+\+json', media)):
            raise _HTTPError(415, 'media_type', 'A supported Content-Type is required.')
        charsets = []
        for parameter in parameters:
            name, separator, value = parameter.strip().partition('=')
            if name.strip() == 'charset':
                charsets.append(value.strip().strip('"') if separator else '')
        if len(charsets) > 1 or any(value not in ('utf-8', 'utf8') for value in charsets):
            raise _HTTPError(415, 'media_type', 'Request text must use UTF-8.')

    async def body(self, *, max_size=None):
        if max_size is not None and (type(max_size) is not int or max_size < 1):
            raise ConfigurationError('A body reader limit must be a positive integer.')
        limit = min(self.max_body_size, max_size) if max_size is not None else self.max_body_size
        if self._failure is not None:
            raise self._failure
        if self._body is not None:
            if len(self._body) > limit:
                raise _HTTPError(413, 'body_too_large', 'Request body exceeds the size limit.')
            return self._body
        if self._reading:
            raise ProtocolError('Concurrent body readers are not supported.')
        self._reading = True
        try:
            lengths = [value for name, value in self.scope['headers'] if name.lower() == b'content-length']
            if lengths and (len(lengths) != 1 or not re.fullmatch(rb'[0-9]{1,20}', lengths[0])):
                raise _HTTPError(400, 'length', 'Invalid Content-Length.')
            length = int(lengths[0]) if lengths else None
            if length is not None and length > limit:
                raise _HTTPError(413, 'body_too_large', 'Request body exceeds the size limit.')
            body = bytearray()
            while True:
                event = await self._receive()
                if event['type'] == 'http.disconnect':
                    raise _Disconnected
                if (event['type'] != 'http.request' or type(event.get('body', b'')) is not bytes
                        or type(event.get('more_body', False)) is not bool):
                    raise ProtocolError('Expected an HTTP request event.')
                chunk = event.get('body', b'')
                if len(body) + len(chunk) > limit:
                    raise _HTTPError(413, 'body_too_large', 'Request body exceeds the size limit.')
                body.extend(chunk)
                if not event.get('more_body', False):
                    break
            if length is not None and len(body) != length:
                raise _HTTPError(400, 'length', 'Content-Length does not match the request body.')
            self._body = bytes(body)
            return self._body
        except BaseException as exc:
            self._failure = exc
            raise
        finally:
            self._reading = False

    async def json(self):
        from .asgi import _check_json, _constant, _pairs
        self._media_type('application/json')
        body = await self.body()
        try:
            data = json.loads(body.decode('utf-8'), object_pairs_hook=_pairs, parse_constant=_constant)
            _check_json(data)
            return data
        except (ValueError, UnicodeError, RecursionError):
            raise _HTTPError(400, 'json', 'Invalid JSON body.') from None

    async def form(self):
        self._media_type('application/x-www-form-urlencoded')
        return _fields(await self.body(), limit=self.max_body_size, max_fields=128, code='form')

    def multipart(self, *, limits=None):
        """Return an async context manager owning all parsed upload files."""
        from .multipart import multipart
        return multipart(self, limits=limits)
