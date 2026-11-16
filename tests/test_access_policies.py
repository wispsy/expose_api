import unittest

from expose_api import App, Authenticated, BearerAuth, ConfigurationError, JSONResponse, Principal, Response, RouteDocs, Router
from asgi_client import request


class AccessPolicyTests(unittest.IsolatedAsyncioTestCase):
    async def test_identity_scopes_tenant_and_checks_precede_body(self):
        identity, calls = [None], []
        async def permission(context):
            calls.append('check')
            self.assertIs(context.principal, identity[0])
            return True
        app = App(authenticate=lambda context: identity[0], auth_challenge='Session')
        @app.route('/admin', methods=('POST',), access=Authenticated(scopes={'admin'}, tenant=True, checks=(permission,)))
        async def handler(context):
            calls.append('handler')
            return JSONResponse(await context.json())
        for principal, status in ((None, 401), (Principal('a'), 403),
                                  (Principal('a', scopes=frozenset({'admin'})), 403),
                                  (Principal('a', tenant='a'), 403)):
            identity[0] = principal
            result = await request(app, '/admin', 'POST', data={})
            self.assertEqual((result.status, result.reads, result.headers[b'cache-control']), (status, 0, b'no-store'))
            self.assertEqual(calls, [])
            if status == 401:
                self.assertEqual(result.headers[b'www-authenticate'], b'Session')
        identity[0] = Principal('a', 'tenant', frozenset({'admin', 'extra'}))
        result = await request(app, '/admin', 'POST', data={'ready': True})
        self.assertEqual((result.status, result.json, result.headers[b'cache-control']), (200, {'ready': True}, b'no-store'))
        self.assertEqual(calls, ['check', 'handler'])

    async def test_fixed_tenant_and_short_circuit_checks(self):
        for tenant in (None, 'other', 'expected'):
            calls = []
            app = App(authenticate=lambda context: Principal('a', tenant), auth_challenge='Custom')
            def deny(context):
                calls.append('deny')
                return False
            policy = Authenticated(tenant='expected', checks=(deny, lambda r: calls.append('unreachable')))
            app.route('/x', access=policy)(lambda r: Response())
            result = await request(app, '/x')
            self.assertEqual(result.status, 403)
            self.assertEqual(calls, ['deny'] if tenant == 'expected' else [])

    async def test_provider_permissions_and_no_store_exception_boundary(self):
        app = App(authenticate=lambda r: Principal('a'), auth_challenge='Session')
        app.provide(bool, lambda r: True)
        async def permission(context):
            return await context.resolve(bool)
        @app.route('/x', access=Authenticated(checks=(permission,)))
        def handler(context):
            raise ValueError('private secret')
        @app.exception_handler(ValueError)
        def recover(context, error):
            return Response(status=409, headers={'cache-control': 'public'})
        result = await request(app, '/x')
        self.assertEqual((result.status, result.headers[b'cache-control']), (409, b'no-store'))

    async def test_router_policy_snapshot_and_openapi(self):
        scopes = {'admin'}
        router = Router(prefix='/api')
        router.route('/x', access=Authenticated(scopes=scopes, tenant=True), name='x')(lambda r: Response())
        scopes.add('other')
        app = App(authenticate=BearerAuth(lambda token: Principal('a', 'tenant', frozenset({'admin'}))), auth_challenge='Bearer')
        app.include_router(router, prefix='/v1')
        result = await request(app, '/v1/api/x', headers=[(b'authorization', b'Bearer valid')])
        self.assertEqual(result.status, 200)
        operation = app.openapi()['paths']['/v1/api/x']['get']
        self.assertEqual(operation['security'], [{'BearerAuth': []}])
        self.assertEqual(operation['x-access-policy'], 'authenticated-policy')
        self.assertEqual(operation['x-required-scopes'], ['admin'])
        self.assertEqual(operation['x-tenant-policy'], 'required')
        self.assertIn('401', operation['responses'])

    async def test_invalid_check_result_is_redacted(self):
        app = App(authenticate=lambda r: Principal('a'), auth_challenge='Custom')
        app.route('/x', access=Authenticated(checks=(lambda r: 1,)))(lambda r: Response())
        with self.assertLogs('expose_api', 'ERROR'):
            result = await request(app, '/x')
        self.assertEqual((result.status, result.reads), (500, 0))
        self.assertEqual(result.headers[b'cache-control'], b'no-store')

    def test_invalid_and_conflicting_configuration_fails_early(self):
        for kwargs in ({'scopes': 'admin'}, {'scopes': {''}}, {'tenant': 1}, {'checks': (None,)}):
            with self.assertRaises(ConfigurationError):
                Authenticated(**kwargs)
        for kwargs in ({'public': True}, {'authorize': lambda r: True}, {'authenticate': True}):
            with self.assertRaises(ConfigurationError):
                Router().route('/x', access=Authenticated(), **kwargs)(lambda r: Response())
        with self.assertRaises(ConfigurationError):
            App().route('/x', access=Authenticated())(lambda r: Response())
        router = Router()
        router.route('/x', access=Authenticated())(lambda r: Response())
        with self.assertRaises(ConfigurationError):
            App().include_router(router)
        for security in ([], [{}], [{'Session': []}, {}]):
            with self.assertRaises(ConfigurationError):
                Router().route('/x', access=Authenticated(), docs=RouteDocs(security=security))(lambda r: Response())
