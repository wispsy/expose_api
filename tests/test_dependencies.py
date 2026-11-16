import asyncio
import unittest
from contextlib import asynccontextmanager, contextmanager

from expose_api import App, ConfigurationError, HTTPException, JSONResponse, Response, StreamingResponse
from asgi_client import request


class Service:
    pass


class Other:
    pass


class DependencyTests(unittest.IsolatedAsyncioTestCase):
    async def test_typed_cache_concurrent_resolution_and_request_isolation(self):
        app, calls = App(), []
        async def provider(context):
            await asyncio.sleep(0)
            calls.append(context)
            return Service()
        app.provide(Service, provider)
        @app.route('/x', public=True)
        async def handler(context):
            first, second = await asyncio.gather(context.resolve(Service), context.resolve(Service))
            self.assertIs(first, second)
            return Response()
        await asyncio.gather(request(app, '/x'), request(app, '/x'))
        self.assertEqual(len(calls), 2)
        for context in calls:
            with self.assertRaises(ConfigurationError):
                await context.resolve(Service)

    async def test_contexts_live_through_stream_then_close_in_reverse_order(self):
        app, calls = App(), []
        @contextmanager
        def other(context):
            calls.append('other:open')
            yield Other()
            calls.append('other:close')
        @asynccontextmanager
        async def service(context):
            await context.resolve(Other)
            calls.append('service:open')
            yield Service()
            calls.append('service:close')
        app.provide(Other, other)
        app.provide(Service, service)
        @app.route('/x', public=True)
        async def handler(context):
            await context.resolve(Service)
            async def stream():
                self.assertEqual(calls, ['other:open', 'service:open'])
                yield b'body'
            return StreamingResponse(stream())
        self.assertEqual((await request(app, '/x', hold=True)).body, b'body')
        self.assertEqual(calls, ['other:open', 'service:open', 'service:close', 'other:close'])

    async def test_exception_handler_uses_same_scope_and_context_sees_error(self):
        app, errors, values = App(), [], []
        @contextmanager
        def provider(context):
            try:
                yield Service()
            except HTTPException as error:
                errors.append(error.status)
                raise
        app.provide(Service, provider)
        @app.exception_handler(HTTPException)
        async def recover(context, error):
            values.append(await context.resolve(Service))
            return Response(status=409)
        @app.route('/x', public=True)
        async def handler(context):
            values.append(await context.resolve(Service))
            raise HTTPException(409, 'Conflict')
        self.assertEqual((await request(app, '/x')).status, 409)
        self.assertIs(values[0], values[1])
        self.assertEqual(errors, [409])

    async def test_failed_provider_is_cached_and_wrong_types_close(self):
        app, calls = App(), []
        @contextmanager
        def provider(context):
            try:
                calls.append('open')
                yield Other()
            finally:
                calls.append('close')
        app.provide(Service, provider)
        @app.route('/x', public=True)
        async def handler(context):
            for _ in range(2):
                with self.assertRaises(ConfigurationError):
                    await context.resolve(Service)
            return Response()
        await request(app, '/x')
        self.assertEqual(calls, ['open', 'close'])

    async def test_unmatched_route_exception_handler_owns_resolved_resources(self):
        app, closed = App(), []
        @contextmanager
        def provider(context):
            try:
                yield Service()
            finally:
                closed.append(True)
        app.provide(Service, provider)
        @app.exception_handler(Exception)
        async def recover(context, error):
            await context.resolve(Service)
            return Response(status=404)
        self.assertEqual((await request(app, '/missing')).status, 404)
        self.assertEqual(closed, [True])

    async def test_dynamic_and_concurrent_cycles_fail_without_deadlock(self):
        for concurrent in (False, True):
            app, ready = App(), asyncio.Event()
            async def service(context):
                if concurrent:
                    await ready.wait()
                await context.resolve(Other)
                return Service()
            async def other(context):
                ready.set()
                await context.resolve(Service)
                return Other()
            app.provide(Service, service)
            app.provide(Other, other)
            @app.route('/x', public=True)
            async def handler(context):
                if concurrent:
                    await asyncio.gather(context.resolve(Service), context.resolve(Other))
                else:
                    await context.resolve(Service)
                return Response()
            with self.assertLogs('expose_api', 'ERROR'):
                result = await asyncio.wait_for(request(app, '/x'), 1)
            self.assertEqual(result.status, 500)

    async def test_nested_overrides_are_task_local_and_restored(self):
        app = App()
        app.provide(str, lambda context: 'original')
        @app.route('/x', public=True)
        async def handler(context):
            return JSONResponse(await context.resolve(str))
        async def selected(value):
            with app.override_providers({str: lambda context: value}):
                await asyncio.sleep(0)
                return (await request(app, '/x')).json
        self.assertEqual(await asyncio.gather(selected('one'), selected('two')), ['one', 'two'])
        self.assertEqual((await request(app, '/x')).json, 'original')
        with app.override_providers({str: lambda context: 'outer'}):
            with app.override_providers({str: lambda context: 'inner'}):
                self.assertEqual((await request(app, '/x')).json, 'inner')
            self.assertEqual((await request(app, '/x')).json, 'outer')

    async def test_repeated_cancellation_settles_cleanup(self):
        app, started, release, closed = App(), asyncio.Event(), asyncio.Event(), []
        @asynccontextmanager
        async def provider(context):
            try:
                yield Service()
            finally:
                started.set()
                await release.wait()
                closed.append(True)
        app.provide(Service, provider)
        @app.route('/x', public=True)
        async def handler(context):
            await context.resolve(Service)
            return Response()
        task = asyncio.create_task(request(app, '/x'))
        await started.wait()
        for _ in range(3):
            task.cancel()
            await asyncio.sleep(0)
            self.assertFalse(task.done())
        release.set()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(closed, [True])

    async def test_cancel_pending_provider_and_transport_error_cleanup(self):
        app, entered, closed = App(), asyncio.Event(), []
        @asynccontextmanager
        async def provider(context):
            try:
                yield Service()
            finally:
                closed.append(True)
        app.provide(Service, provider)
        @app.route('/x', public=True)
        async def handler(context):
            await context.resolve(Service)
            return Response()
        async def send(event):
            raise OSError('closed')
        with self.assertRaises(OSError):
            await app({'type': 'http', 'method': 'GET', 'path': '/x'}, None, send)
        self.assertEqual(closed, [True])

    async def test_provider_registration_freezes_and_missing_resolution_fails(self):
        app = App()
        for key, provider in (('name', lambda r: Service()), (Service, None)):
            with self.assertRaises(ConfigurationError):
                app.provide(key, provider)
        app.provide(Service, lambda r: Service())
        with self.assertRaises(ConfigurationError):
            app.provide(Service, lambda r: Service())
        @app.route('/x', public=True)
        async def handler(context):
            with self.assertRaises(ConfigurationError):
                await context.resolve(Other)
            return Response()
        await request(app, '/x')
        with self.assertRaises(ConfigurationError):
            app.provide(Other, lambda r: Other())
        with self.assertRaises(ConfigurationError):
            with app.override_providers({Other: lambda r: Other()}):
                pass
