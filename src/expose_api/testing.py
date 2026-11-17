"""Dependency-free asynchronous ASGI application testing utilities."""

import asyncio
import json as _json
import re
from contextlib import ExitStack
from dataclasses import dataclass
from email.message import Message
from http.cookiejar import CookieJar
from urllib.parse import quote, unquote, urlencode, urlsplit
from urllib.request import Request as URLRequest

from .errors import ConfigurationError
from .http_protocol import ProtocolError, _settle_task
from .requests import MultiDict
from .responses import _headers

_UNSET = object()


async def _next(queue, task, timeout):
    reader = asyncio.create_task(queue.get())
    try:
        done, _ = await asyncio.wait((reader, task), timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
        if task in done and not task.cancelled() and task.exception() is not None:
            task.result()
        if reader in done:
            return reader.result()
        if task in done:
            task.result()
            raise ProtocolError('Application stopped before the expected event.')
        raise TimeoutError('Timed out waiting for an ASGI event.')
    finally:
        reader.cancel()
        await asyncio.gather(reader, return_exceptions=True)


@dataclass(frozen=True, slots=True)
class TestResponse:
    status: int
    raw_headers: tuple
    body: bytes
    messages: tuple
    reads: int
    complete: bool

    @property
    def headers(self):
        return MultiDict((key.decode('ascii').lower(), value.decode('latin-1')) for key, value in self.raw_headers)

    @property
    def text(self):
        return self.body.decode('utf-8')

    @property
    def json(self):
        return _json.loads(self.body)


class TestClient:
    """Use as an async context manager; requests share one explicit lifespan."""

    def __init__(self, app, *, base_url='http://testserver', root_path='', headers=(),
                 providers=None, authenticate=None, timeout=5, max_response_size=16 * 1024 * 1024):
        origin = urlsplit(base_url)
        if (not callable(app) or origin.scheme not in ('http', 'https') or not origin.hostname
                or origin.username is not None or origin.password is not None
                or origin.path not in ('', '/') or origin.query or origin.fragment
                or type(root_path) is not str or root_path and not root_path.startswith('/')
                or root_path.startswith('//') or any(c in root_path for c in '\\?#')
                or type(timeout) not in (int, float) or not 0 < timeout <= 300
                or type(max_response_size) is not int or max_response_size < 1):
            raise ConfigurationError('Select a local test origin, root path and positive limits.')
        self.app, self.base_url, self.root_path = app, base_url.rstrip('/'), root_path.rstrip('/')
        self.timeout, self.max_response_size = timeout, max_response_size
        self.headers, self.providers, self.authenticate = _headers(headers), providers, authenticate
        self.cookies, self.state = CookieJar(), {}
        self._stack, self._lifespan, self._active = None, None, False
        self._requests, self._sockets = set(), set()

    async def __aenter__(self):
        if self._stack is not None:
            raise ConfigurationError('TestClient cannot be entered twice.')
        self._stack = ExitStack()
        try:
            if self.providers is not None:
                self._stack.enter_context(self.app.override_providers(self.providers))
            if self.authenticate is not None:
                self._stack.enter_context(self.app.override_authentication(self.authenticate))
            self._incoming, self._outgoing = asyncio.Queue(), asyncio.Queue()
            scope = {'type': 'lifespan', 'asgi': {'version': '3.0', 'spec_version': '2.0'}, 'state': self.state}
            self._lifespan = asyncio.create_task(self.app(scope, self._incoming.get, self._outgoing.put))
            await self._incoming.put({'type': 'lifespan.startup'})
            event = await _next(self._outgoing, self._lifespan, self.timeout)
            if event.get('type') != 'lifespan.startup.complete':
                raise ProtocolError('Application lifespan startup failed.')
            self._active = True
            return self
        except BaseException:
            await self._close()
            raise

    async def _close(self):
        self._active = False
        async def drain():
            tasks = [*self._requests, *(socket.task for socket in self._sockets if socket.task is not None)]
            for task in tasks:
                if not task.done():
                    task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            if self._lifespan is not None and not self._lifespan.done():
                self._lifespan.cancel()
                await asyncio.gather(self._lifespan, return_exceptions=True)
        task = asyncio.create_task(drain())
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            await _settle_task(task)
            raise
        finally:
            if self._stack is not None:
                self._stack.close()
                self._stack = None

    async def __aexit__(self, kind, error, traceback):
        try:
            for socket in tuple(self._sockets):
                await socket.close()
            for task in self._requests:
                task.cancel()
            if self._requests:
                await asyncio.gather(*self._requests, return_exceptions=True)
            await self._incoming.put({'type': 'lifespan.shutdown'})
            event = await _next(self._outgoing, self._lifespan, self.timeout)
            if event.get('type') != 'lifespan.shutdown.complete':
                raise ProtocolError('Application lifespan shutdown failed.')
            await asyncio.wait_for(asyncio.shield(self._lifespan), self.timeout)
        finally:
            await self._close()

    def _scope(self, path, headers, kind='http'):
        if not self._active:
            raise ConfigurationError('Enter TestClient before making requests.')
        if (type(path) is not str or not path.startswith('/') or path.startswith('//')
                or any(c in '\\#' or ord(c) < 32 or ord(c) == 127 for c in path)):
            raise ConfigurationError('Test request paths must be local absolute paths.')
        parsed = urlsplit(path)
        if parsed.scheme or parsed.netloc or re.search(r'%(?![0-9a-fA-F]{2})', parsed.path):
            raise ConfigurationError('Invalid test request path.')
        url = self.base_url + quote(parsed.path, safe='/%:@') + ('?' + parsed.query if parsed.query else '')
        cookie_request = URLRequest(url)
        self.cookies.add_cookie_header(cookie_request)
        selected = [*self.headers, *_headers(headers)]
        origin = urlsplit(self.base_url)
        if not any(name == b'host' for name, _ in selected):
            selected.append((b'host', origin.netloc.encode('ascii')))
        if not any(name == b'cookie' for name, _ in selected) and cookie_request.has_header('Cookie'):
            selected.append((b'cookie', cookie_request.get_header('Cookie').encode('latin-1')))
        scheme = origin.scheme if kind == 'http' else 'wss' if origin.scheme == 'https' else 'ws'
        scope = {'type': kind, 'asgi': {'version': '3.0', 'spec_version': '2.5'}, 'http_version': '1.1',
                 'scheme': scheme, 'path': unquote(parsed.path, encoding='utf-8', errors='strict'),
                 'raw_path': quote(parsed.path, safe='/%:@').encode('ascii'), 'root_path': self.root_path,
                 'query_string': parsed.query.encode('ascii'), 'headers': selected, 'state': self.state.copy(),
                 'server': (origin.hostname, origin.port or (443 if origin.scheme == 'https' else 80)),
                 'client': ('127.0.0.1', 50000)}
        return scope, cookie_request

    async def request(self, method, path='/', *, headers=(), content=b'', json=_UNSET, data=None,
                      params=None, events=None, disconnect=False, scope=None):
        if type(method) is not str or not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Z-]+", method):
            raise ConfigurationError('Select an uppercase HTTP method.')
        if sum((json is not _UNSET, data is not None, content != b'', events is not None)) > 1:
            raise ConfigurationError('Select one request body representation.')
        selected_headers = _headers(headers)
        if params is not None:
            path += ('&' if '?' in path else '?') + urlencode(params, doseq=True)
        if json is not _UNSET:
            content = _json.dumps(json, allow_nan=False).encode('utf-8')
            selected_headers.append((b'content-type', b'application/json'))
        elif data is not None:
            content = urlencode(data, doseq=True).encode('ascii')
            selected_headers.append((b'content-type', b'application/x-www-form-urlencoded'))
        if type(content) is str:
            content = content.encode('utf-8')
        if type(content) is not bytes:
            raise ConfigurationError('Request content must be bytes or text.')
        events = tuple(events) if events is not None else None
        request_scope, cookie_request = self._scope(path, selected_headers)
        request_scope.update(scope or {})
        request_scope['method'] = method
        pending = asyncio.Queue()
        for event in events if events is not None else ({'type': 'http.request', 'body': content},):
            pending.put_nowait(event)
        if disconnect:
            pending.put_nowait({'type': 'http.disconnect'})
        messages, chunks, reads, size = [], [], 0, 0
        started, complete = False, False
        async def receive():
            nonlocal reads
            reads += 1
            return await pending.get()
        async def send(event):
            nonlocal started, complete, size
            if not isinstance(event, dict):
                raise ProtocolError('Invalid test response event.')
            kind = event.get('type')
            if kind == 'http.response.start':
                if started or type(event.get('status')) is not int or not 200 <= event['status'] <= 599:
                    raise ProtocolError('Invalid response start.')
                started = True
                event = {**event, 'headers': _headers(event.get('headers', ()))}
            elif kind == 'http.response.body':
                if (not started or complete or type(event.get('body', b'')) is not bytes
                        or type(event.get('more_body', False)) is not bool):
                    raise ProtocolError('Invalid response body.')
                chunk = event.get('body', b'')
                size += len(chunk)
                if size > self.max_response_size:
                    raise ProtocolError('Test response exceeds the buffer limit.')
                chunks.append(chunk)
                complete = not event.get('more_body', False)
            else:
                raise ProtocolError('Unsupported test response event.')
            messages.append(dict(event))
        task = asyncio.create_task(self.app(request_scope, receive, send))
        self._requests.add(task)
        try:
            await asyncio.wait_for(task, self.timeout)
        finally:
            self._requests.discard(task)
        if not complete and not disconnect and not any(event.get('type') == 'http.disconnect' for event in (events or ())):
            raise ProtocolError('Application returned an incomplete response.')
        raw_headers = tuple(messages[0]['headers']) if started else ()
        lengths = [value for name, value in raw_headers if name == b'content-length']
        if (len(lengths) > 1 or lengths and not re.fullmatch(rb'[0-9]{1,20}', lengths[0])
                or complete and method != 'HEAD' and lengths and int(lengths[0]) != size
                or started and messages[0]['status'] in (204, 205, 304) and size):
            raise ProtocolError('Response framing does not match the buffered body.')
        cookie_headers = Message()
        for name, value in raw_headers:
            cookie_headers.add_header(name.decode('ascii'), value.decode('latin-1'))
        class CookieResponse:
            def info(self):
                return cookie_headers
        self.cookies.extract_cookies(CookieResponse(), cookie_request)
        return TestResponse(messages[0]['status'] if started else 0, raw_headers,
                            b''.join(chunks), tuple(messages), reads, complete)

    async def get(self, path='/', **options):
        return await self.request('GET', path, **options)

    async def post(self, path='/', **options):
        return await self.request('POST', path, **options)

    async def put(self, path='/', **options):
        return await self.request('PUT', path, **options)

    async def patch(self, path='/', **options):
        return await self.request('PATCH', path, **options)

    async def delete(self, path='/', **options):
        return await self.request('DELETE', path, **options)

    async def head(self, path='/', **options):
        return await self.request('HEAD', path, **options)

    async def options(self, path='/', **options):
        return await self.request('OPTIONS', path, **options)

    def websocket(self, path, *, headers=(), subprotocols=()):
        return WebSocketSession(self, path, headers, subprotocols)


class WebSocketSession:
    def __init__(self, client, path, headers, subprotocols):
        self.client, self.path, self.headers = client, path, headers
        self.subprotocols = tuple(subprotocols)
        self.incoming, self.outgoing = asyncio.Queue(), asyncio.Queue(maxsize=1)
        self.task, self.accepted, self.closed = None, None, False
        self._started, self._sent_close = False, False

    async def _send(self, event):
        kind = event.get('type')
        if kind == 'websocket.accept':
            if self._started or self._sent_close:
                raise ProtocolError('Duplicate WebSocket acceptance.')
            self._started = True
        elif kind == 'websocket.send':
            text, data = event.get('text'), event.get('bytes')
            if (not self._started or self._sent_close or (text is None) == (data is None)
                    or text is not None and type(text) is not str or data is not None and type(data) is not bytes):
                raise ProtocolError('Invalid WebSocket message.')
            size = len(text.encode('utf-8')) if text is not None else len(data)
            if size > self.client.max_response_size:
                raise ProtocolError('Test WebSocket message exceeds the buffer limit.')
        elif kind == 'websocket.close':
            if self._sent_close:
                raise ProtocolError('Duplicate WebSocket close.')
            self._sent_close = True
        else:
            raise ProtocolError('Unsupported WebSocket event.')
        await self.outgoing.put(dict(event))

    async def __aenter__(self):
        scope, _ = self.client._scope(self.path, self.headers, 'websocket')
        scope['subprotocols'] = list(self.subprotocols)
        self.task = asyncio.create_task(self.client.app(scope, self.incoming.get, self._send))
        self.client._sockets.add(self)
        await self.incoming.put({'type': 'websocket.connect'})
        try:
            self.accepted = await self.receive_event()
            if self.accepted.get('type') != 'websocket.accept':
                raise ProtocolError('WebSocket was not accepted.')
            return self
        except BaseException:
            await self.close()
            raise

    async def receive_event(self):
        if self.task is None:
            raise ConfigurationError('Enter a WebSocket session before receiving.')
        event = await _next(self.outgoing, self.task, self.client.timeout)
        if event.get('type') not in ('websocket.accept', 'websocket.send', 'websocket.close'):
            raise ProtocolError('Unsupported WebSocket event.')
        return event

    async def send_text(self, value):
        if self.task is None or self.task.done() or self.closed or type(value) is not str:
            raise ConfigurationError('Send text only on an active WebSocket session.')
        await self.incoming.put({'type': 'websocket.receive', 'text': value})

    async def send_bytes(self, value):
        if self.task is None or self.task.done() or self.closed or type(value) is not bytes:
            raise ConfigurationError('Send bytes only on an active WebSocket session.')
        await self.incoming.put({'type': 'websocket.receive', 'bytes': value})

    async def send_json(self, value):
        await self.send_text(_json.dumps(value, allow_nan=False))

    async def receive_json(self):
        event = await self.receive_event()
        if event['type'] != 'websocket.send':
            raise ProtocolError('WebSocket closed before a JSON message.')
        return _json.loads(event.get('text', event.get('bytes', b'')))

    async def close(self, code=1000):
        if self.closed:
            return
        self.closed = True
        try:
            if self.task is not None:
                await self.incoming.put({'type': 'websocket.disconnect', 'code': code})
                async def discard():
                    while True:
                        await self.outgoing.get()
                draining = asyncio.create_task(discard())
                try:
                    await asyncio.wait_for(self.task, self.client.timeout)
                finally:
                    draining.cancel()
                    await asyncio.gather(draining, return_exceptions=True)
                    if not self.task.done():
                        self.task.cancel()
                        await asyncio.gather(self.task, return_exceptions=True)
        finally:
            self.client._sockets.discard(self)

    async def __aexit__(self, *args):
        await self.close()
