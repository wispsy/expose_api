"""Explicit byte responses for ordinary HTTP handlers."""

import json
import re
from datetime import datetime, timezone
from email.utils import format_datetime
from http.cookies import SimpleCookie
from urllib.parse import quote

from .errors import ConfigurationError

_TOKEN = re.compile(rb"[!#$%&'*+.^_`|~0-9A-Za-z-]+")
_EMPTY = frozenset({204, 205, 304})


def _headers(values):
    result = []
    for name, value in values.items() if isinstance(values, dict) else values:
        try:
            name = name.encode('ascii') if isinstance(name, str) else name
            value = value.encode('latin-1') if isinstance(value, str) else value
            if (type(name) is not bytes or not _TOKEN.fullmatch(name)
                    or type(value) is not bytes or any(byte < 32 or byte == 127 for byte in value)):
                raise ValueError
        except (UnicodeError, ValueError):
            raise ConfigurationError('Invalid HTTP response header.') from None
        result.append((name.lower(), value))
    return result


class Response:
    """An explicit response; repeated headers retain their order."""

    def __init__(self, content=b'', *, status=200, headers=(), media_type=None):
        if type(status) is not int or not 200 <= status <= 599:
            raise ConfigurationError('Response status must be between 200 and 599.')
        if isinstance(content, str):
            content = content.encode('utf-8')
        if type(content) is not bytes:
            raise ConfigurationError('Response content must be bytes or text.')
        if status in _EMPTY and content:
            raise ConfigurationError('This response status does not permit content.')
        self.status, self.body = status, content
        self.headers = _headers(headers)
        lengths = [value for key, value in self.headers if key == b'content-length']
        if (any(key == b'transfer-encoding' for key, _ in self.headers)
                or len(lengths) > 1 or lengths and lengths != [str(len(content)).encode()]
                or status in (204, 304) and lengths):
            raise ConfigurationError('Response framing is owned by the response.')
        if not lengths and status not in (204, 304):
            self.headers.append((b'content-length', str(len(content)).encode()))
        if media_type is not None and not any(key == b'content-type' for key, _ in self.headers):
            self.headers.extend(_headers((('content-type', media_type),)))

    def set_cookie(self, name, value='', *, max_age=None, expires=None, path='/',
                   domain=None, secure=False, httponly=False, samesite='lax'):
        """Append a Set-Cookie header; values are quoted by the standard library."""
        if (type(name) is not str or not name or not _TOKEN.fullmatch(name.encode('ascii', errors='replace'))
                or type(value) is not str or any(ord(char) < 32 or ord(char) >= 127 for char in value)
                or type(secure) is not bool or type(httponly) is not bool
                or samesite not in ('lax', 'strict', 'none', None)
                or max_age is not None and type(max_age) is not int):
            raise ConfigurationError('Invalid cookie options.')
        for option in (path, domain):
            if option is not None and (type(option) is not str or any(ord(c) < 33 or ord(c) >= 127 or c == ';' for c in option)):
                raise ConfigurationError('Invalid cookie path or domain.')
        if (name.startswith('__Secure-') and not secure
                or name.startswith('__Host-') and (not secure or path != '/' or domain is not None)
                or samesite == 'none' and not secure):
            raise ConfigurationError('Cookie prefix or SameSite options require secure attributes.')
        if expires is not None and (not isinstance(expires, datetime) or expires.utcoffset() is None):
            raise ConfigurationError('Cookie expiry must be a timezone-aware datetime.')
        cookie = SimpleCookie()
        try:
            cookie[name] = value
        except Exception:
            raise ConfigurationError('Invalid cookie name or value.') from None
        morsel = cookie[name]
        for key, option in (('max-age', max_age), ('path', path), ('domain', domain),
                            ('secure', secure), ('httponly', httponly), ('samesite', samesite)):
            if option is not None:
                morsel[key] = option
        if expires is not None:
            morsel['expires'] = format_datetime(expires.astimezone(timezone.utc), usegmt=True)
        self.headers.extend(_headers((('set-cookie', morsel.OutputString()),)))

    def delete_cookie(self, name, **options):
        self.set_cookie(name, '', max_age=0, expires=datetime(1970, 1, 1, tzinfo=timezone.utc), **options)

    async def prepare(self, scope):
        """Resolve a response before headers start; subclasses may own preflight I/O."""
        return self

    async def __call__(self, scope, receive, send):
        await send({'type': 'http.response.start', 'status': self.status, 'headers': list(self.headers)})
        await send({'type': 'http.response.body', 'body': b'' if scope['method'] == 'HEAD' else self.body})


class JSONResponse(Response):
    def __init__(self, content, *, status=200, headers=()):
        body = json.dumps(content, ensure_ascii=False, allow_nan=False, separators=(',', ':')).encode('utf-8')
        super().__init__(body, status=status, headers=headers, media_type='application/json')


class HTMLResponse(Response):
    def __init__(self, content, *, status=200, headers=()):
        super().__init__(content, status=status, headers=headers, media_type='text/html; charset=utf-8')


class RedirectResponse(Response):
    def __init__(self, url, *, status=307, headers=()):
        if type(url) is not str or any(ord(c) < 32 or ord(c) == 127 for c in url):
            raise ConfigurationError('Invalid redirect URL.')
        if status not in (301, 302, 303, 307, 308):
            raise ConfigurationError('Choose a redirect response status.')
        super().__init__(status=status, headers=[*_headers(headers),
                         (b'location', quote(url, safe=":/?#[]@!$&'()*+,;=%").encode('ascii'))])


class HTTPException(Exception):
    """An intentionally public HTTP failure; detail is safe to send to clients."""

    def __init__(self, status, detail, *, headers=()):
        if type(status) is not int or not 400 <= status <= 599 or type(detail) is not str:
            raise ConfigurationError('HTTPException requires an error status and public text.')
        self.status, self.detail, self.headers = status, detail, _headers(headers)
        if any(name in (b'content-length', b'transfer-encoding') for name, _ in self.headers):
            raise ConfigurationError('HTTPException framing is owned by the error response.')
        super().__init__(detail)
