"""Single-use byte streams with explicit iterator and disconnect ownership."""

import asyncio
import inspect

from .errors import ConfigurationError
from .http_protocol import ProtocolError
from .responses import Response, _headers


class StreamingResponse(Response):
    """Own one nonblocking byte iterator, closing it on every exit path."""

    def __init__(self, content, *, status=200, headers=(), media_type=None,
                 chunk_size=65536, content_length=None):
        if (type(chunk_size) is not int or not 1 <= chunk_size <= 1024 * 1024
                or content_length is not None and (type(content_length) is not int or content_length < 0)
                or status in (204, 205, 304) or isinstance(content, (str, bytes, bytearray))):
            raise ConfigurationError('Choose a byte iterator, bounded chunks and a content-bearing status.')
        headers = _headers(headers)
        if any(name == b'content-length' for name, _ in headers):
            raise ConfigurationError('Select stream length with content_length.')
        super().__init__(status=status, headers=headers, media_type=media_type)
        self.headers = [(name, value) for name, value in self.headers if name != b'content-length']
        if content_length is not None:
            self.headers.append((b'content-length', str(content_length).encode('ascii')))
        try:
            self._async = hasattr(content, '__aiter__')
            self._iterator = content.__aiter__() if self._async else iter(content)
            if self._async and not hasattr(self._iterator, '__anext__'):
                raise TypeError
        except TypeError:
            raise ConfigurationError('Content must supply a synchronous or asynchronous byte iterator.') from None
        self.chunk_size, self.content_length, self._used = chunk_size, content_length, False

    async def _close(self):
        close = getattr(self._iterator, 'aclose', None) or getattr(self._iterator, 'close', None)
        if close is not None:
            result = close()
            if inspect.isawaitable(result):
                await result

    async def _stream(self, send):
        sent = 0
        while True:
            try:
                chunk = await anext(self._iterator) if self._async else next(self._iterator)
            except (StopIteration, StopAsyncIteration):
                break
            if type(chunk) is not bytes:
                raise ProtocolError('Response streams must yield bytes.')
            sent += len(chunk)
            if self.content_length is not None and sent > self.content_length:
                raise ProtocolError('Response stream exceeds its declared length.')
            for offset in range(0, len(chunk), self.chunk_size):
                await send({'type': 'http.response.body', 'body': chunk[offset:offset + self.chunk_size], 'more_body': True})
            await asyncio.sleep(0)
        if self.content_length is not None and sent != self.content_length:
            raise ProtocolError('Response stream does not match its declared length.')
        await send({'type': 'http.response.body', 'body': b'', 'more_body': False})

    @staticmethod
    async def _disconnect(receive):
        while True:
            event = await receive()
            if event['type'] == 'http.disconnect':
                return
            if event['type'] != 'http.request':
                raise ProtocolError('Expected an HTTP request or disconnect event.')

    async def __call__(self, scope, receive, send):
        if self._used:
            raise ConfigurationError('Streaming responses are single-use.')
        self._used = True
        tasks = []
        try:
            await send({'type': 'http.response.start', 'status': self.status, 'headers': list(self.headers)})
            if scope['method'] == 'HEAD':
                await send({'type': 'http.response.body', 'body': b''})
                return
            sender = asyncio.create_task(self._stream(send))
            listener = asyncio.create_task(self._disconnect(receive))
            tasks = [sender, listener]
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            if sender.done():
                sender.result()
            else:
                listener.result()
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await self._close()
