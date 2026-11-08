import asyncio
import unittest

from expose_api import App, ConfigurationError, ProtocolError, StreamingResponse, serve
from asgi_client import request


class Source:
    def __init__(self, chunks=(), *, block=False):
        self.chunks = iter(chunks)
        self.started = asyncio.Event()
        self.closed = False
        self.block = block
        self.iterations = 0

    def __aiter__(self):
        return self

    async def __anext__(self):
        self.iterations += 1
        self.started.set()
        if self.block:
            await asyncio.Future()
        try:
            return next(self.chunks)
        except StopIteration:
            raise StopAsyncIteration from None

    async def aclose(self):
        self.closed = True


class StreamingTests(unittest.IsolatedAsyncioTestCase):
    async def test_chunks_length_headers_and_sync_iterator(self):
        app = App()
        app.route('/stream', public=True)(lambda r: StreamingResponse(iter([b'abcdef', b'gh']), chunk_size=3,
                                                                     content_length=8, media_type='text/plain'))
        result = await request(app, '/stream', hold=True)
        self.assertEqual((result.body, result.headers[b'content-length']), (b'abcdefgh', b'8'))
        self.assertTrue(all(len(event.get('body', b'')) <= 3 for event in result.messages[1:]))
        self.assertTrue(all(event['more_body'] for event in result.messages[1:-1]))
        self.assertFalse(result.messages[-1]['more_body'])

    async def test_head_does_not_iterate_and_closes_source(self):
        source = Source([b'secret'])
        result = await request(StreamingResponse(source, content_length=6), method='HEAD')
        self.assertEqual((result.body, source.iterations, source.closed), (b'', 0, True))
        self.assertEqual(result.headers[b'content-length'], b'6')

    async def test_disconnect_cancellation_and_send_failures_close_source(self):
        for mode in ('disconnect', 'cancel', 'start', 'body'):
            source = Source([b'one'], block=mode in ('disconnect', 'cancel'))
            response = StreamingResponse(source)
            events = []
            async def receive():
                await source.started.wait()
                if mode == 'disconnect':
                    return {'type': 'http.disconnect'}
                await asyncio.Future()
            async def send(event):
                events.append(event)
                if event['type'] == 'http.response.' + mode:
                    raise OSError('closed')
            if mode == 'cancel':
                task = asyncio.create_task(response({'method': 'GET'}, receive, send))
                await source.started.wait()
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
            elif mode in ('start', 'body'):
                with self.assertRaises(OSError):
                    await response({'method': 'GET'}, receive, send)
            else:
                await response({'method': 'GET'}, receive, send)
            self.assertTrue(source.closed, mode)
            self.assertEqual(sum(event['type'] == 'http.response.start' for event in events), 1)

    async def test_wrong_chunks_lengths_and_producer_failure_never_restart(self):
        for chunks, length in (([b'long'], 2), ([b'x'], 2), (['text'], None)):
            source = Source(chunks)
            app = App()
            app.route('/stream', public=True)(lambda r: StreamingResponse(source, content_length=length))
            events = []
            async def receive():
                await asyncio.Future()
            async def send(event):
                events.append(event)
            with self.assertRaises(ProtocolError):
                await app({'type': 'http', 'method': 'GET', 'path': '/stream'}, receive, send)
            self.assertTrue(source.closed)
            self.assertEqual(sum(event['type'] == 'http.response.start' for event in events), 1)
        closed = []
        async def broken():
            try:
                yield b'first'
                raise ValueError('producer error')
            finally:
                closed.append(True)
        with self.assertRaises(ValueError):
            await request(StreamingResponse(broken()), hold=True)
        self.assertEqual(closed, [True])

    async def test_exception_handler_can_stream_and_protected_headers_remain(self):
        app = App()
        @app.route('/fail', authorize=lambda r: True)
        def fail(context):
            raise LookupError('private')
        @app.exception_handler(LookupError)
        def handle(context, exc):
            return StreamingResponse([b'handled'], status=409)
        result = await request(app, '/fail', hold=True)
        self.assertEqual((result.status, result.body, result.headers[b'cache-control']), (409, b'handled', b'no-store'))

    async def test_real_development_server_stream(self):
        app = App()
        app.route('/stream', public=True)(lambda r: StreamingResponse([b'one', b'two']))
        async with serve(app, port=0) as server:
            reader, writer = await asyncio.open_connection('127.0.0.1', server.sockets[0].getsockname()[1])
            try:
                writer.write(b'GET /stream HTTP/1.1\r\nHost: localhost\r\n\r\n')
                await writer.drain()
                raw = await reader.read()
                self.assertEqual(raw.split(b'\r\n\r\n', 1)[1], b'onetwo')
            finally:
                writer.close()
                await writer.wait_closed()

    def test_configuration(self):
        for options in ({'chunk_size': 0}, {'content_length': True}, {'status': 204},
                        {'headers': {'content-length': '0'}}):
            with self.assertRaises(ConfigurationError):
                StreamingResponse([], **options)
        with self.assertRaises(ConfigurationError):
            StreamingResponse(b'bytes')
