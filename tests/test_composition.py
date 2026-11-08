import asyncio
import unittest
from contextlib import asynccontextmanager
from dataclasses import dataclass

from expose_api import App, ConfigurationError, JSONResponse, MemoryStore, Response, Router
from asgi_client import request


class CompositionTests(unittest.IsolatedAsyncioTestCase):
    async def test_middleware_order_constructed_once_and_head(self):
        app, calls, built = App(), [], []
        def wrapper(inner, *, name):
            built.append(name)
            async def middleware(scope, receive, send):
                calls.append(name + ':before')
                async def decorate(event):
                    if event['type'] == 'http.response.start':
                        event = {**event, 'headers': [*event['headers'], (b'x-' + name.encode(), b'yes')]}
                    await send(event)
                await inner(scope, receive, decorate)
                calls.append(name + ':after')
            return middleware
        app.add_middleware(wrapper, name='outer')
        app.add_middleware(wrapper, name='inner')
        app.route('/x', public=True)(lambda r: Response(b'body'))
        result = await request(app, '/x')
        self.assertEqual(calls, ['outer:before', 'inner:before', 'inner:after', 'outer:after'])
        self.assertEqual(result.headers[b'x-outer'], b'yes')
        self.assertEqual((await request(app, '/x', 'HEAD')).body, b'')
        self.assertEqual(built, ['inner', 'outer'])
        with self.assertRaises(ConfigurationError):
            app.add_middleware(wrapper, name='late')

    async def test_mounted_scopes_boundaries_and_isolation(self):
        app, child, seen = App(), App(), []
        @child.route('/{tail:path}', public=True, name='child')
        def handler(context):
            seen.append(context.scope)
            context.state['child'] = True
            return JSONResponse({'url': context.url_for('child', tail=context.path_params['tail'])})
        app.mount('/child', child)
        self.assertEqual((await request(app, '/base/child/café', root_path='/base')).json,
                         {'url': '/base/child/caf%C3%A9'})
        self.assertEqual((await request(app, '/child/café', root_path='/base')).json,
                         {'url': '/base/child/caf%C3%A9'})
        self.assertEqual((await request(app, '/childish/café')).status, 404)
        scope = {'type': 'http', 'method': 'GET', 'path': '/base/child/a', 'root_path': '/base',
                 'raw_path': b'/base/child/a', 'query_string': b'x=one%20two', 'state': {'parent': True}}
        events = []
        async def send(event):
            events.append(event)
        await app(scope, None, send)
        self.assertEqual(seen[-1]['raw_path'], b'/base/child/a')
        self.assertEqual(seen[-1]['query_string'], b'x=one%20two')
        self.assertEqual(scope['root_path'], '/base')
        self.assertEqual(scope['state'], {'parent': True})
        with self.assertRaises(ConfigurationError):
            app.mount('/late', child)

    async def test_parent_owns_lifespan_and_middleware_observes_it(self):
        calls = []
        @asynccontextmanager
        async def parent_life(app):
            calls.append('parent:start')
            yield
            calls.append('parent:stop')
        @asynccontextmanager
        async def child_life(app):
            calls.append('child:start')
            yield
        app = App(lifespan=parent_life)
        app.mount('/child', App(lifespan=child_life))
        pending = [{'type': 'lifespan.startup'}, {'type': 'lifespan.shutdown'}]
        sent = []
        async def receive():
            return pending.pop(0)
        async def send(event):
            sent.append(event['type'])
        await app({'type': 'lifespan'}, receive, send)
        self.assertEqual(calls, ['parent:start', 'parent:stop'])
        self.assertEqual(sent, ['lifespan.startup.complete', 'lifespan.shutdown.complete'])

    async def test_failures_before_and_after_start_do_not_double_send(self):
        before = App()
        def fail(inner):
            async def middleware(scope, receive, send):
                raise ValueError('private credential')
            return middleware
        before.add_middleware(fail)
        with self.assertLogs('expose_api', 'ERROR') as logs:
            result = await request(before, '/')
        self.assertEqual(result.status, 500)
        self.assertNotIn('credential', str(logs.output) + result.body.decode())
        after = App()
        async def broken(scope, receive, send):
            await send({'type': 'http.response.start', 'status': 200, 'headers': []})
            raise RuntimeError('after start')
        after.mount('/child', broken)
        events = []
        async def send(event):
            events.append(event)
        with self.assertRaises(RuntimeError):
            await after({'type': 'http', 'method': 'GET', 'path': '/child'}, None, send)
        self.assertEqual(len(events), 1)
        direct = App()
        direct.route('/x', public=True)(lambda r: Response(b'x'))
        for fail_at in ('http.response.start', 'http.response.body'):
            events.clear()
            async def failed_send(event):
                events.append(event)
                if event['type'] == fail_at:
                    raise OSError('closed')
            with self.assertRaises(OSError):
                await direct({'type': 'http', 'method': 'GET', 'path': '/x'}, None, failed_send)
            self.assertEqual(sum(e['type'] == 'http.response.start' for e in events), 1)

    async def test_mount_cancellation_propagates(self):
        started = asyncio.Event()
        async def child(scope, receive, send):
            started.set()
            await asyncio.Future()
        app = App()
        app.mount('/child', child)
        task = asyncio.create_task(request(app, '/child'))
        await started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

    def test_collision_checks_in_both_directions(self):
        @dataclass
        class Note:
            id: str
        for existing in ('route', 'resource', 'mount'):
            app = App()
            if existing == 'route':
                app.route('/notes/{identity}', public=True)(lambda r: Response())
            elif existing == 'resource':
                app.include(Note, store=MemoryStore(), public=True)
            else:
                app.mount('/notes', App())
            with self.assertRaises(ConfigurationError):
                app.mount('/notes', App())
        app = App()
        app.mount('/notes', App())
        with self.assertRaises(ConfigurationError):
            app.route('/notes/x', public=True)(lambda r: Response())
        with self.assertRaises(ConfigurationError):
            app.include(Note, store=MemoryStore(), public=True)
        router = Router()
        router.route('/notes/x', public=True)(lambda r: Response())
        with self.assertRaises(ConfigurationError):
            app.include_router(router)
