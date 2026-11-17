import asyncio
import unittest
from contextlib import asynccontextmanager
from dataclasses import dataclass

from expose_api import App, Authenticated, ConfigurationError, JSONResponse, MemoryStore, Principal, ProtocolError, Response, StreamingResponse, TestClient
from expose_api.websocket import SUBPROTOCOL


@dataclass
class ClientNote:
    id: str
    title: str = 'first'


class TestingTests(unittest.IsolatedAsyncioTestCase):
    async def test_lifecycle_json_forms_repeated_headers_and_head(self):
        calls = []
        @asynccontextmanager
        async def lifespan(app):
            calls.append('start')
            yield
            calls.append('stop')
        app = App(lifespan=lifespan)
        @app.route('/echo', methods=('POST',), public=True)
        async def echo(context):
            if context.header('content-type') == 'application/json':
                return JSONResponse(await context.json(), headers=[('x-a', 'one'), ('x-a', 'two')])
            return JSONResponse((await context.form()).getlist('x'))
        app.route('/health', public=True)(lambda context: Response(b'ok'))
        async with TestClient(app) as client:
            self.assertEqual(calls, ['start'])
            response = await client.post('/echo', json={'ready': True})
            self.assertEqual((response.status, response.json, response.complete), (200, {'ready': True}, True))
            self.assertEqual(response.headers.getlist('x-a'), ['one', 'two'])
            self.assertEqual((await client.post('/echo', data=[('x', 'one'), ('x', 'two')])).json, ['one', 'two'])
            self.assertEqual((await client.head('/health')).body, b'')
        self.assertEqual(calls, ['start', 'stop'])
        with self.assertRaises(ConfigurationError):
            await client.get('/health')

    async def test_cookie_path_secure_expiry_and_repeated_set_cookie(self):
        app = App()
        @app.route('/set', public=True)
        def set_cookies(context):
            response = Response()
            response.set_cookie('plain', 'one')
            response.set_cookie('private', 'two', path='/private')
            response.set_cookie('secure_cookie', 'three', secure=True)
            response.set_cookie('expired', 'four', max_age=0)
            response.set_cookie('foreign', 'five', domain='other.invalid')
            return response
        @app.route('/{path:path}', public=True)
        def read(context):
            return JSONResponse(context.cookies)
        async with TestClient(app) as client:
            self.assertEqual(len((await client.get('/set')).headers.getlist('set-cookie')), 5)
            self.assertEqual((await client.get('/echo')).json, {'plain': 'one'})
            self.assertEqual((await client.get('/private/x')).json, {'plain': 'one', 'private': 'two'})
        async with TestClient(app, base_url='https://testserver') as client:
            await client.get('/set')
            self.assertEqual((await client.get('/echo')).json, {'plain': 'one', 'secure_cookie': 'three'})

    async def test_overrides_restore_and_do_not_bypass_authorization(self):
        app = App(authenticate=lambda r: None, auth_challenge='Session')
        app.provide(str, lambda r: 'original')
        @app.route('/private', access=Authenticated(scopes={'read'}))
        async def private(context):
            return JSONResponse(await context.resolve(str))
        async with TestClient(app, providers={str: lambda r: 'replacement'},
                              authenticate=lambda r: Principal('a', scopes=frozenset({'read'}))) as client:
            self.assertEqual((await client.get('/private')).json, 'replacement')
        async with TestClient(app, authenticate=lambda r: Principal('a')) as client:
            self.assertEqual((await client.get('/private')).status, 403)
        async with TestClient(app) as client:
            self.assertEqual((await client.get('/private')).status, 401)
        self.assertEqual(app._provider_overrides.get(), {})
        self.assertIsNone(app._authentication_override.get())

    async def test_resources_and_websocket_subscription(self):
        app = App(authenticate=lambda r: None, auth_challenge='Session')
        resource = app.include(ClientNote, store=MemoryStore(events=8), path='/notes',
                               authorize=lambda r: True, record_policy=lambda p, op, value: True)
        app.subscribe(resource, origins=('http://testserver',), hosts=('testserver',), poll_interval=0.01)
        async with TestClient(app, authenticate=lambda r: Principal('a', 'tenant')) as client:
            self.assertEqual((await client.post('/notes', json={'id': 'a'})).status, 201)
            async with client.websocket('/notes/a', headers={'origin': 'http://testserver'}, subprotocols=(SUBPROTOCOL,)) as socket:
                self.assertEqual(socket.accepted['subprotocol'], SUBPROTOCOL)
                await socket.send_json({'v': 1, 'type': 'subscribe', 'cursor': None})
                self.assertEqual((await socket.receive_json())['value'], {'id': 'a', 'title': 'first'})
                await client.patch('/notes/a', json={'title': 'updated'})
                self.assertEqual((await socket.receive_json())['value']['title'], 'updated')
        self.assertEqual(app._sockets.tasks, {})

    async def test_disconnect_and_streaming_cleanup(self):
        app, closed = App(), []
        async def source():
            try:
                yield b'first'
                await asyncio.Future()
            finally:
                closed.append(True)
        app.route('/stream', public=True)(lambda r: StreamingResponse(source()))
        @app.route('/body', methods=('POST',), public=True)
        async def body(context):
            return Response(await context.body())
        async with TestClient(app) as client:
            result = await client.post('/body', events=[{'type': 'http.disconnect'}])
            self.assertEqual((result.status, result.complete), (0, False))
            result = await client.get('/stream', disconnect=True)
            self.assertFalse(result.complete)
        # A disconnect can arrive before the generator is first iterated.
        self.assertLessEqual(len(closed), 1)

    async def test_bounded_response_and_timeouts_propagate(self):
        app = App()
        app.route('/big', public=True)(lambda r: Response(b'12345'))
        @app.route('/blocked', public=True)
        async def blocked(context):
            await asyncio.Future()
        async with TestClient(app, timeout=0.05, max_response_size=4) as client:
            with self.assertRaises(ProtocolError):
                await client.get('/big')
            with self.assertRaises(TimeoutError):
                await client.get('/blocked')
            self.assertEqual(client._requests, set())

    async def test_raw_asgi_state_path_and_application_error(self):
        seen = []
        async def raw(scope, receive, send):
            if scope['type'] == 'lifespan':
                await receive()
                scope['state']['service'] = 'ready'
                await send({'type': 'lifespan.startup.complete'})
                await receive()
                await send({'type': 'lifespan.shutdown.complete'})
            else:
                seen.append(scope)
                if scope['path'] == '/fail':
                    raise ValueError('visible to test')
                await send({'type': 'http.response.start', 'status': 200, 'headers': []})
                await send({'type': 'http.response.body', 'body': b'body'})
        async with TestClient(raw, root_path='/base') as client:
            await client.get('/caf%C3%A9?q=one%20two')
            self.assertEqual(seen[-1]['path'], '/café')
            self.assertEqual(seen[-1]['raw_path'], b'/caf%C3%A9')
            self.assertEqual(seen[-1]['root_path'], '/base')
            self.assertEqual(seen[-1]['state'], {'service': 'ready'})
            with self.assertRaisesRegex(ValueError, 'visible to test'):
                await client.get('/fail')
            with self.assertRaises(ConfigurationError):
                await client.get('//other.invalid/path')

    async def test_failed_lifespan_restores_overrides(self):
        @asynccontextmanager
        async def fail(app):
            raise RuntimeError('private startup')
            yield
        app = App(lifespan=fail, authenticate=lambda r: None, auth_challenge='Custom')
        app.provide(str, lambda r: 'original')
        with self.assertLogs('expose_api', 'ERROR'):
            with self.assertRaises(ProtocolError):
                async with TestClient(app, providers={str: lambda r: 'test'}, authenticate=lambda r: Principal('a')):
                    pass
        self.assertEqual(app._provider_overrides.get(), {})
        self.assertIsNone(app._authentication_override.get())

    async def test_websocket_refusal_and_application_failure_are_visible(self):
        async def app(scope, receive, send):
            if scope['type'] == 'lifespan':
                await receive()
                await send({'type': 'lifespan.startup.complete'})
                await receive()
                await send({'type': 'lifespan.shutdown.complete'})
            else:
                await receive()
                if scope['path'] == '/fail':
                    raise RuntimeError('websocket failure')
                await send({'type': 'websocket.close', 'code': 1008})
        async with TestClient(app) as client:
            with self.assertRaises(ProtocolError):
                async with client.websocket('/deny'):
                    pass
            with self.assertRaisesRegex(RuntimeError, 'websocket failure'):
                async with client.websocket('/fail'):
                    pass
            self.assertEqual(client._sockets, set())

    async def test_exit_cancels_unfinished_requests_before_shutdown(self):
        app, started, stopped = App(), asyncio.Event(), []
        @app.route('/blocked', public=True)
        async def blocked(context):
            started.set()
            try:
                await asyncio.Future()
            finally:
                stopped.append(True)
        async with TestClient(app) as client:
            task = asyncio.create_task(client.get('/blocked'))
            await started.wait()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(stopped, [True])
