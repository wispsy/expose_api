import asyncio
import unittest

from expose_api import App, BearerAuth, ConfigurationError, HTTPException, JSONResponse, Principal, ProtocolError, Response, Router
from asgi_client import request


class RouteAccessTests(unittest.IsolatedAsyncioTestCase):
    def protected_app(self, *, verify=None, authorize=None, dependencies=()):
        app = App(authenticate=BearerAuth(verify or (lambda token: Principal('user', scopes=frozenset({'write'}))
                                                   if token == 'valid' else None)), auth_challenge='Bearer')
        @app.route('/private', methods=('POST',), authenticate=True,
                   authorize=authorize or (lambda r: 'write' in r.principal.scopes), dependencies=dependencies)
        async def handler(context):
            return JSONResponse({'subject': context.principal.subject, 'data': await context.json()},
                                headers={'cache-control': 'public'})
        return app

    async def test_authentication_authorization_and_error_order(self):
        app = self.protected_app()
        missing = await request(app, '/private', 'POST', body=b'bad')
        self.assertEqual((missing.status, missing.reads, missing.headers[b'www-authenticate']), (401, 0, b'Bearer'))
        repeated = await request(app, '/private', 'POST', headers=[(b'authorization', b'Bearer valid')] * 2)
        self.assertEqual((repeated.status, repeated.reads), (401, 0))
        denied = self.protected_app(authorize=lambda r: False)
        result = await request(denied, '/private', 'POST', headers=[(b'authorization', b'Bearer valid')], body=b'bad')
        self.assertEqual((result.status, result.reads), (403, 0))
        accepted = await request(app, '/private', 'POST', headers=[(b'authorization', b'Bearer valid')], data={'x': 1})
        self.assertEqual(accepted.json, {'subject': 'user', 'data': {'x': 1}})
        self.assertEqual([v for k, v in accepted.messages[0]['headers'] if k == b'cache-control'], [b'no-store'])
        malformed = await request(app, '/private', 'POST', headers=[(b'authorization', b'Bearer valid'),
                                                                   (b'content-type', b'application/json')], body=b'bad')
        self.assertEqual((malformed.status, malformed.headers[b'cache-control']), (400, b'no-store'))
        for response in (missing, repeated, result):
            self.assertEqual(response.headers[b'cache-control'], b'no-store')
        for method, status in (('PUT', 405), ('OPTIONS', 204)):
            result = await request(app, '/private', method)
            self.assertEqual((result.status, result.reads, result.headers[b'cache-control']), (status, 0, b'no-store'))

    async def test_ordered_dependencies_once_and_early_failure(self):
        calls = []
        def first(context):
            calls.append(('first', context.principal.subject))
            context.state['checked'] = True
        async def second(context):
            self.assertTrue(context.state['checked'])
            calls.append(('second', context.principal.subject))
        app = self.protected_app(dependencies=(first, second, first))
        await request(app, '/private', 'POST', headers=[(b'authorization', b'Bearer valid')], data={})
        self.assertEqual(calls, [('first', 'user'), ('second', 'user')])
        def deny(context):
            raise HTTPException(409, 'Try again later.')
        denied = self.protected_app(dependencies=(deny, second))
        result = await request(denied, '/private', 'POST', headers=[(b'authorization', b'Bearer valid')])
        self.assertEqual((result.status, result.reads), (409, 0))
        self.assertEqual(len(calls), 2)

    async def test_exception_handlers_select_nearest_type_and_keep_context(self):
        class CustomFailure(ValueError):
            pass
        app = self.protected_app()
        @app.exception_handler(Exception)
        def broad(context, exc):
            return Response(b'broad', status=500)
        @app.exception_handler(ValueError)
        async def specific(context, exc):
            return JSONResponse({'subject': context.principal.subject, 'checked': context.state['checked']},
                                status=409, headers={'cache-control': 'public'})
        def fail(context):
            context.state['checked'] = True
            raise CustomFailure('private submitted content')
        router = Router(prefix='/api')
        router.route('/fail', authorize=lambda r: True, authenticate=True, dependencies=(fail,))(lambda r: Response())
        app.include_router(router)
        result = await request(app, '/api/fail', headers=[(b'authorization', b'Bearer valid')])
        self.assertEqual((result.status, result.json), (409, {'subject': 'user', 'checked': True}))
        self.assertEqual(result.headers[b'cache-control'], b'no-store')
        with self.assertRaises(ConfigurationError):
            app.exception_handler(LookupError)(specific)

    async def test_hook_and_exception_handler_failures_are_redacted(self):
        for options in ({'verify': lambda token: 'invalid principal'}, {'authorize': lambda r: 1},
                        {'dependencies': (lambda r: 'invalid result',)}):
            app = self.protected_app(**options)
            with self.assertLogs('expose_api', 'ERROR'):
                result = await request(app, '/private', 'POST', headers=[(b'authorization', b'Bearer valid')])
            self.assertEqual((result.status, result.reads, result.headers[b'cache-control']), (500, 0, b'no-store'))
        app = App()
        @app.exception_handler(ValueError)
        def failed_handler(context, exc):
            raise RuntimeError('credential secret')
        @app.route('/fail', public=True)
        def fail(context):
            raise ValueError('submitted secret')
        with self.assertLogs('expose_api', 'ERROR') as logs:
            result = await request(app, '/fail')
        self.assertEqual(result.status, 500)
        self.assertNotIn('secret', result.body.decode() + str(logs.output))

    async def test_middleware_errors_use_handlers_and_protected_cache_state(self):
        app = self.protected_app()
        @app.exception_handler(LookupError)
        def expected(context, exc):
            return Response(b'recovered', status=409, headers={'cache-control': 'public'})
        def intercept(inner):
            async def middleware(scope, receive, send):
                async def discard(event):
                    pass
                await inner(scope, receive, discard)
                raise LookupError('private')
            return middleware
        app.add_middleware(intercept)
        result = await request(app, '/private', 'POST', headers=[(b'authorization', b'Bearer valid')], data={})
        self.assertEqual((result.status, result.body, result.headers[b'cache-control']), (409, b'recovered', b'no-store'))

    async def test_cancellation_protocol_and_send_failures_bypass_handlers(self):
        started = asyncio.Event()
        async def blocking(context):
            started.set()
            await asyncio.Future()
        app = self.protected_app(dependencies=(blocking,))
        handled = []
        @app.exception_handler(Exception)
        def unexpected(context, exc):
            handled.append(True)
            return Response(status=500)
        task = asyncio.create_task(request(app, '/private', 'POST', headers=[(b'authorization', b'Bearer valid')]))
        await started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        direct = self.protected_app()
        with self.assertRaises(ProtocolError):
            await request(direct, '/private', 'POST', headers=[(b'authorization', b'Bearer valid'),
                                                            (b'content-type', b'application/json')],
                          events=[{'type': 'invalid'}])
        self.assertEqual(handled, [])
        wire = App()
        wire.exception_handler(Exception)(unexpected)
        wire.route('/x', public=True)(lambda r: Response(b'body'))
        sent = []
        async def failed_send(event):
            sent.append(event)
            raise OSError('closed')
        with self.assertRaises(OSError):
            await wire({'type': 'http', 'method': 'GET', 'path': '/x'}, None, failed_send)
        self.assertEqual((len(sent), handled), (1, []))

    def test_registration_gates(self):
        with self.assertRaises(ConfigurationError):
            App().route('/private', authenticate=True, authorize=lambda r: True)(lambda r: Response())
        router = Router()
        router.route('/private', authenticate=True, authorize=lambda r: True)(lambda r: Response())
        with self.assertRaises(ConfigurationError):
            App().include_router(router)
        for kind in (BaseException, ProtocolError, 'wrong'):
            with self.assertRaises(ConfigurationError):
                App().exception_handler(kind)(lambda r, e: Response())
