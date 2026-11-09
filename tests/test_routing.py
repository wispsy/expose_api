import asyncio
import unittest
from dataclasses import dataclass
from uuid import UUID

from expose_api import App, ConfigurationError, HTTPException, JSONResponse, MemoryStore, Response, Router
from asgi_client import request


@dataclass
class Note:
    id: str


class RoutingTests(unittest.IsolatedAsyncioTestCase):
    async def test_health_json_and_sync_async_handlers(self):
        app = App()
        @app.route('/healthz', public=True)
        def health(context):
            return JSONResponse({'ready': True, 'method': context.method})
        @app.route('/echo', methods=('POST',), public=True)
        async def echo(context):
            return JSONResponse(await context.json(), status=201)
        self.assertEqual((await request(app, '/healthz')).json, {'ready': True, 'method': 'GET'})
        self.assertEqual((await request(app, '/echo', 'POST', data=[1, 2])).json, [1, 2])
        self.assertEqual((await request(app, '/healthz/')).status, 404)
        with self.assertRaises(ConfigurationError):
            app.route('/late', public=True)(health)

    async def test_converters_order_and_root_path(self):
        app = App()
        @app.route('/items/latest', public=True)
        def latest(context):
            return JSONResponse({'latest': True})
        @app.route('/items/{number:int}', public=True)
        def number(context):
            self.assertIs(type(context.path_params['number']), int)
            return JSONResponse(context.path_params)
        @app.route('/ids/{identity:uuid}', public=True)
        def identity(context):
            self.assertIs(type(context.path_params['identity']), UUID)
            return JSONResponse({'id': str(context.path_params['identity'])})
        @app.route('/pages/{name:path}', public=True)
        def pages(context):
            return JSONResponse(context.path_params)
        self.assertEqual((await request(app, '/prefix/items/10', root_path='/prefix')).json, {'number': 10})
        self.assertEqual((await request(app, '/items/latest')).json, {'latest': True})
        for value in ('-1', 'text', '9' * 100):
            self.assertEqual((await request(app, '/items/' + value)).status, 404)
        self.assertEqual((await request(app, '/ids/00000000-0000-0000-0000-000000000001')).status, 200)
        self.assertEqual((await request(app, '/ids/invalid')).status, 404)
        self.assertEqual((await request(app, '/pages/a/café')).json, {'name': 'a/café'})

    async def test_methods_allow_and_explicit_head(self):
        app = App()
        app.route('/same', public=True)(lambda r: Response(b'get'))
        app.route('/same', methods=('POST', 'PURGE'), public=True)(lambda r: Response(status=204))
        app.route('/same', methods=('HEAD',), public=True)(lambda r: Response(b'head', headers={'x-explicit': 'yes'}))
        result = await request(app, '/same', 'HEAD')
        self.assertEqual((result.body, result.headers[b'x-explicit']), (b'', b'yes'))
        options = await request(app, '/same', 'OPTIONS')
        self.assertEqual(options.headers[b'allow'], b'GET, HEAD, OPTIONS, POST, PURGE')
        self.assertEqual((await request(app, '/same', 'PUT')).headers[b'allow'], options.headers[b'allow'])
        self.assertEqual((await request(app, '/absent', 'POST')).status, 404)
        fallback = App()
        fallback.route('/read', public=True)(lambda r: Response(b'body'))
        self.assertEqual((await request(fallback, '/read', 'HEAD')).headers[b'content-length'], b'4')

    async def test_authorization_before_body_and_public_errors(self):
        app = App()
        app.route('/private', methods=('POST',), authorize=lambda r: False)(lambda r: Response())
        result = await request(app, '/private', 'POST', body=b'invalid')
        self.assertEqual((result.status, result.reads, result.headers[b'cache-control']), (403, 0, b'no-store'))
        errors = App()
        @errors.route('/error', public=True)
        def expected(context):
            raise HTTPException(409, 'Public conflict.', headers={'x-reason': 'conflict'})
        result = await request(errors, '/error')
        self.assertEqual((result.status, result.json['error']['message']), (409, 'Public conflict.'))
        bad = App()
        bad.route('/bad', public=True)(lambda r: {'not': 'a response'})
        with self.assertLogs('expose_api', 'ERROR'):
            self.assertEqual((await request(bad, '/bad')).status, 500)

    async def test_malformed_media_parameters_fail_before_body_dispatch(self):
        app = App()
        @app.route('/echo', methods=('POST',), public=True)
        async def echo(context):
            return JSONResponse(await context.json())
        result = await request(app, '/echo', 'POST', body=b'{}',
                               headers=[(b'content-type', b'application/json; charset="utf-8')])
        self.assertEqual((result.status, result.reads, result.json['error']['code']), (415, 0, 'media_type'))

    async def test_resource_and_tabular_namespaces_remain_owned(self):
        app = App()
        app.include(Note, store=MemoryStore(), public=True)
        app.route('/', public=True)(lambda r: Response(b'home'))
        app.route('/healthz', public=True)(lambda r: Response())
        for path in ('/notes', '/notes/{id}', '/notes/_table/query', '/{any:path}'):
            with self.assertRaises(ConfigurationError):
                app.route(path, public=True)(lambda r: Response())
        self.assertEqual((await request(app, '/notes', 'POST', data={'id': 'one'})).status, 201)
        self.assertEqual((await request(app, '/notes')).json, [{'id': 'one'}])
        other = App()
        other.route('/notes/{id}', public=True)(lambda r: Response())
        with self.assertRaises(ConfigurationError):
            other.include(Note, store=MemoryStore(), public=True)

    def test_registration_validation(self):
        handler = lambda r: Response()
        for path in ('relative', '//host', '/bad/{a:float}', '/{a}/{a}', '/{tail:path}/after', '/a/../b', '/bad//path'):
            with self.subTest(path=path), self.assertRaises(ConfigurationError):
                App().route(path, public=True)(handler)
        for options in ({}, {'public': True, 'authorize': lambda r: True}, {'methods': 'GET', 'public': True},
                        {'methods': ('bad method',), 'public': True}):
            with self.assertRaises(ConfigurationError):
                App().route('/x', **options)(handler)
        router = Router()
        router.route('/{first}', public=True)(handler)
        with self.assertRaises(ConfigurationError):
            router.route('/{second:str}', public=True)(handler)

    async def test_disconnect_cancellation_and_send_errors(self):
        app = App()
        @app.route('/echo', methods=('POST',), public=True)
        async def echo(context):
            return Response(await context.body())
        self.assertEqual((await request(app, '/echo', 'POST', events=[{'type': 'http.disconnect'}])).messages, [])
        task = asyncio.create_task(request(app, '/echo', 'POST', events=[], hold=True))
        await asyncio.sleep(0)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        events = []
        async def send(event):
            events.append(event)
            raise OSError('closed')
        with self.assertRaises(OSError):
            await app({'type': 'http', 'method': 'GET', 'path': '/missing'}, None, send)
        self.assertEqual(len(events), 1)
