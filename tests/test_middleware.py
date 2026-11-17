import unittest
from contextlib import asynccontextmanager
from dataclasses import dataclass

from expose_api import App, Authenticated, ConfigurationError, CORSMiddleware, CSRFMiddleware, JSONContract, JSONResponse, Principal, ProxyHeadersMiddleware, RateLimitMiddleware, Response, SecurityHeadersMiddleware, StreamingResponse, TestClient, TrustedHostMiddleware
from asgi_client import request


@dataclass
class EchoMessage:
    text: str


class MiddlewareTests(unittest.IsolatedAsyncioTestCase):
    async def test_composed_security_typed_services_contracts_and_client(self):
        app, calls, token = App(authenticate=lambda r: None, auth_challenge='Session'), [], '0123456789abcdef0123456789abcdef'
        app.add_middleware(ProxyHeadersMiddleware, trusted_proxies=('127.0.0.1',))
        app.add_middleware(SecurityHeadersMiddleware, hsts_max_age=3600)
        app.add_middleware(TrustedHostMiddleware, allowed_hosts=('testserver',))
        app.add_middleware(CORSMiddleware, origins=('https://good.test',), methods=('POST',),
                           headers=('x-csrf-token', 'content-type'), credentials=True)
        app.add_middleware(CSRFMiddleware, origins=('https://good.test',), token=lambda r: token)
        app.add_middleware(RateLimitMiddleware, decide=lambda r: None)
        @asynccontextmanager
        async def service(context):
            calls.append('open')
            try:
                yield 'approved'
            finally:
                calls.append('close')
        app.provide(str, service)
        async def permitted(context):
            return await context.resolve(str) == 'approved'
        @app.route('/echo', methods=('POST',), access=Authenticated(scopes={'echo'}, tenant=True, checks=(permitted,)),
                   contract=JSONContract(input=EchoMessage, output=EchoMessage))
        async def echo(context):
            return EchoMessage(context.input.text + ':' + await context.resolve(str))
        async with TestClient(app, authenticate=lambda r: Principal('a', 'tenant', frozenset({'echo'}))) as client:
            headers = {'cookie': 'session=verified', 'origin': 'https://good.test', 'x-csrf-token': token,
                       'x-forwarded-proto': 'https'}
            denied = await client.post('/echo', json={'text': 'hello'}, headers={**headers, 'x-csrf-token': 'bad'})
            self.assertEqual((denied.status, denied.reads, calls), (403, 0, []))
            result = await client.post('/echo', json={'text': 'hello'}, headers=headers)
            self.assertEqual((result.status, result.json), (200, {'text': 'hello:approved'}))
            self.assertEqual(result.headers['cache-control'], 'no-store')
            self.assertEqual(result.headers['strict-transport-security'], 'max-age=3600')
            self.assertEqual(result.headers['access-control-allow-origin'], 'https://good.test')
        self.assertEqual(calls, ['open', 'close'])

    async def test_trusted_hosts_ports_ipv6_wildcards_and_ambiguous_headers(self):
        app = App()
        app.add_middleware(TrustedHostMiddleware, allowed_hosts=('good.test', '*.assets.test', '[::1]'))
        app.route('/x', methods=('POST',), public=True)(lambda r: Response())
        for host in ('GOOD.test', 'good.test:443', 'cdn.assets.test', '[::1]:8000'):
            self.assertEqual((await request(app, '/x', 'POST', headers=[(b'host', host.encode())])).status, 200)
        for headers in ([], [(b'host', b'evil.test')], [(b'host', b'good.test.evil.test')],
                        [(b'host', b'assets.test')], [(b'host', b'good.test@evil.test')],
                        [(b'host', b'good.test:')], [(b'host', b'good.test:99999')],
                        [(b'host', b'good.test/path')], [(b'host', b'good.test\r\ninjected')],
                        [(b'host', b'good.test'), (b'host', b'evil.test')]):
            result = await request(app, '/x', 'POST', headers=headers)
            self.assertEqual((result.status, result.reads), (400, 0))

    async def test_trusted_host_websocket_denial_and_lifespan_passthrough(self):
        app = App()
        app.add_middleware(TrustedHostMiddleware, allowed_hosts=('testserver',))
        events = []
        async def send(event):
            events.append(event)
        await app({'type': 'websocket', 'headers': [(b'host', b'evil.test')]}, None, send)
        self.assertEqual(events, [{'type': 'websocket.close', 'code': 1008}])
        async with TestClient(app):
            pass

    async def test_proxy_trust_chain_stripping_scheme_and_original_scope(self):
        app = App()
        app.add_middleware(ProxyHeadersMiddleware, trusted_proxies=('127.0.0.0/8', '10.0.0.0/8'))
        @app.route('/x', public=True)
        def show(context):
            return JSONResponse({'client': context.scope['client'][0], 'scheme': context.scope['scheme'],
                                 'forwarded': context.header('x-forwarded-for'), 'other': context.header('forwarded')})
        async with TestClient(app) as client:
            result = await client.get('/x', headers={'x-forwarded-for': '192.0.2.99, 198.51.100.2, 10.1.0.2',
                                                    'x-forwarded-proto': 'https', 'forwarded': 'spoof'})
            self.assertEqual(result.json, {'client': '198.51.100.2', 'scheme': 'https', 'forwarded': None, 'other': None})
            result = await client.get('/x', headers={'x-forwarded-for': 'not an IP', 'x-forwarded-proto': 'invalid'},
                                      scope={'client': ('203.0.113.7', 123)})
            self.assertEqual(result.json['client'], '203.0.113.7')
            self.assertEqual(result.json['scheme'], 'http')
            for headers in ([('x-forwarded-for', 'unknown')], [('x-forwarded-proto', 'https,http')],
                            [('x-forwarded-for', '1.2.3.4'), ('x-forwarded-for', '5.6.7.8')],
                            [('x-forwarded-for', ','.join(['1.2.3.4'] * 33))]):
                denied = await client.get('/x', headers=headers)
                self.assertEqual((denied.status, denied.reads), (400, 0))

    async def test_security_headers_cover_outer_errors_cookies_streams_and_no_store(self):
        app = App(authenticate=lambda r: Principal('a'), auth_challenge='Session')
        app.add_middleware(SecurityHeadersMiddleware, hsts_max_age=3600)
        @app.route('/x', access=Authenticated())
        def normal(context):
            response = Response(b'body', headers={'cache-control': 'public', 'x-frame-options': 'SAMEORIGIN'})
            response.set_cookie('first', 'one')
            response.set_cookie('second', 'two')
            return response
        @app.route('/stream', public=True)
        def stream(context):
            return StreamingResponse(iter([b'one', b'two']))
        async with TestClient(app, base_url='https://testserver') as client:
            result = await client.get('/x')
            self.assertEqual(result.headers['strict-transport-security'], 'max-age=3600')
            self.assertEqual(result.headers['x-frame-options'], 'DENY')
            self.assertEqual(result.headers['cache-control'], 'no-store')
            self.assertEqual(len(result.headers.getlist('set-cookie')), 2)
            self.assertEqual((await client.get('/stream')).body, b'onetwo')
        async with TestClient(app) as client:
            self.assertNotIn('strict-transport-security', (await client.get('/x')).headers)
        failing = App()
        failing.add_middleware(SecurityHeadersMiddleware)
        def fail(inner):
            async def broken(scope, receive, send):
                if scope['type'] == 'http':
                    raise ValueError('private secret')
                await inner(scope, receive, send)
            return broken
        failing.add_middleware(fail)
        with self.assertLogs('expose_api', 'ERROR') as logs:
            result = await request(failing, '/x')
        self.assertEqual((result.status, result.headers[b'x-content-type-options']), (500, b'nosniff'))
        self.assertNotIn('secret', result.body.decode() + str(logs.output))

    async def test_security_headers_work_with_an_independent_asgi_app(self):
        async def inner(scope, receive, send):
            await Response(b'ok')(scope, receive, send)
        result = await request(SecurityHeadersMiddleware(inner), '/x')
        self.assertEqual(result.headers[b'x-frame-options'], b'DENY')

    async def test_cors_preflight_has_explicit_methods_headers_and_vary(self):
        app, calls = App(), []
        app.add_middleware(CORSMiddleware, origins=('https://good.test',), methods=('POST',),
                           headers=('content-type', 'x-csrf-token'), credentials=True)
        app.route('/x', methods=('POST',), public=True)(lambda r: calls.append(True))
        async with TestClient(app) as client:
            result = await client.options('/x', headers={'origin': 'https://good.test',
                                                        'access-control-request-method': 'POST',
                                                        'access-control-request-headers': 'Content-Type, X-CSRF-Token'})
            self.assertEqual((result.status, result.body, result.reads), (204, b'', 0))
            self.assertEqual(result.headers['access-control-allow-origin'], 'https://good.test')
            self.assertEqual(result.headers['access-control-allow-credentials'], 'true')
            self.assertEqual(result.headers['access-control-allow-methods'], 'POST')
            self.assertIn('Access-Control-Request-Headers', result.headers['vary'])
            for change in ({'origin': 'https://evil.test'}, {'access-control-request-method': 'DELETE'},
                           {'access-control-request-headers': 'authorization'}, {'origin': 'null'}):
                headers = {'origin': 'https://good.test', 'access-control-request-method': 'POST', **change}
                self.assertEqual((await client.options('/x', headers=headers)).status, 403)
        self.assertEqual(calls, [])

    async def test_cors_actual_responses_errors_and_hostile_origins(self):
        app = App(authenticate=lambda r: Principal('a'), auth_challenge='Session')
        app.add_middleware(CORSMiddleware, origins=('https://good.test',), methods=('GET',), credentials=True, expose_headers=('etag',))
        @app.route('/x', access=Authenticated())
        def normal(context):
            return Response(headers=[('vary', 'Accept-Encoding'), ('set-cookie', 'one=1'), ('set-cookie', 'two=2')])
        @app.route('/fail', public=True)
        def fail(context):
            raise ValueError('secret')
        async with TestClient(app) as client:
            result = await client.get('/x', headers={'origin': 'https://good.test'})
            self.assertEqual(result.headers['vary'], 'Accept-Encoding, Origin')
            self.assertEqual(result.headers['cache-control'], 'no-store')
            self.assertEqual(result.headers['access-control-expose-headers'], 'etag')
            self.assertEqual(len(result.headers.getlist('set-cookie')), 2)
            with self.assertLogs('expose_api', 'ERROR'):
                error = await client.get('/fail', headers={'origin': 'https://good.test'})
            self.assertEqual((error.status, error.headers['access-control-allow-origin']), (500, 'https://good.test'))
        for value in (b'https://good.test.evil.test', b'https://good.test@evil.test',
                      b'https://good.test\n', b'https://good.test#', b'https://good.test/', b'null'):
            result = await request(app, '/x', headers=[(b'origin', value)])
            self.assertEqual((result.status, result.reads), (403, 0))
        self.assertEqual((await request(app, '/x', headers=[(b'origin', b'https://good.test')] * 2)).status, 403)

    async def test_cors_headers_also_cover_middleware_callback_failure(self):
        app = App()
        app.add_middleware(CORSMiddleware, origins=('https://good.test',))
        app.add_middleware(RateLimitMiddleware, decide=lambda request: True)
        with self.assertLogs('expose_api', 'ERROR'):
            result = await request(app, '/x', headers=[(b'origin', b'https://good.test')])
        self.assertEqual((result.status, result.headers[b'access-control-allow-origin']), (500, b'https://good.test'))

    async def test_csrf_requires_session_bound_token_and_origin_before_body(self):
        app, calls, token = App(), [], '0123456789abcdef0123456789abcdef'
        async def expected(context):
            calls.append(context.cookies['session'])
            return token
        app.add_middleware(CSRFMiddleware, origins=('https://good.test',), token=expected)
        @app.route('/x', methods=('GET', 'POST'), public=True)
        async def handler(context):
            return Response(await context.body())
        async with TestClient(app) as client:
            base = {'cookie': 'session=verified', 'origin': 'https://good.test', 'x-csrf-token': token}
            good = await client.post('/x', headers=base, content=b'body')
            self.assertEqual((good.status, good.body), (200, b'body'))
            for change in ({'origin': 'https://evil.test'}, {'x-csrf-token': 'wrongwrongwrongwrong'}, {'x-csrf-token': ''}):
                denied = await client.post('/x', headers={**base, **change}, content=b'body')
                self.assertEqual((denied.status, denied.reads), (403, 0))
            self.assertEqual((await client.post('/x', content=b'no cookie')).status, 200)
            self.assertEqual((await client.get('/x', headers={'cookie': 'session=verified'})).status, 200)
        self.assertEqual(len(calls), 2)  # accepted token and the nonmatching, well-formed token

    async def test_csrf_duplicate_cookies_headers_referer_and_always_profile(self):
        token = '0123456789abcdef0123456789abcdef'
        app = App()
        app.add_middleware(CSRFMiddleware, origins=('https://good.test',), token=lambda r: token, always=True)
        app.route('/x', methods=('POST',), public=True)(lambda r: Response())
        async with TestClient(app) as client:
            self.assertEqual((await client.post('/x')).status, 403)
            self.assertEqual((await client.post('/x', headers={'referer': 'https://good.test/form?x=1', 'x-csrf-token': token})).status, 200)
            for headers in ([('origin', 'https://good.test'), ('cookie', 'session=a; session=b'), ('x-csrf-token', token)],
                            [('origin', 'https://good.test'), ('cookie', 'session=a'), ('cookie', 'session=b'), ('x-csrf-token', token)],
                            [('origin', 'https://good.test'), ('x-csrf-token', token), ('x-csrf-token', token)],
                            [('referer', 'https://good.test@evil.test/form'), ('x-csrf-token', token)],
                            [('origin', 'null'), ('x-csrf-token', token)]):
                result = await client.post('/x', headers=headers)
                self.assertEqual((result.status, result.reads), (403, 0))

    async def test_missing_or_invalid_expected_csrf_token_and_body_read_callback(self):
        token = '0123456789abcdef0123456789abcdef'
        for expected, status in ((None, 403), (True, 500), (b'\xff' * 16, 500)):
            app = App()
            app.add_middleware(CSRFMiddleware, origins=('https://good.test',), token=lambda r: expected, always=True)
            app.route('/x', methods=('POST',), public=True)(lambda r: Response())
            async with TestClient(app) as client:
                if status == 500:
                    with self.assertLogs('expose_api', 'ERROR'):
                        result = await client.post('/x', headers={'origin': 'https://good.test', 'x-csrf-token': token})
                else:
                    result = await client.post('/x', headers={'origin': 'https://good.test', 'x-csrf-token': token})
                self.assertEqual((result.status, result.reads), (status, 0))
        app = App()
        app.add_middleware(RateLimitMiddleware, decide=lambda r: r.body())
        with self.assertLogs('expose_api', 'ERROR'):
            result = await request(app, '/x')
        self.assertEqual((result.status, result.reads), (500, 0))

    async def test_rate_limit_shared_callback_contract(self):
        for decision, status in ((None, 200), (0, 429), (30, 429), (True, 500), (-1, 500)):
            app = App()
            async def decide(context):
                self.assertEqual(context.path, '/x')
                return decision
            app.add_middleware(RateLimitMiddleware, decide=decide)
            app.route('/x', public=True)(lambda r: Response())
            if status == 500:
                with self.assertLogs('expose_api', 'ERROR'):
                    result = await request(app, '/x')
            else:
                result = await request(app, '/x')
            self.assertEqual((result.status, result.reads), (status, 0))
            if status == 429:
                self.assertEqual(result.headers[b'retry-after'], str(decision).encode())
                self.assertEqual(result.headers[b'cache-control'], b'no-store')

    def test_invalid_security_configuration_fails(self):
        for factory, options in ((TrustedHostMiddleware, {'allowed_hosts': '*'}),
                                 (TrustedHostMiddleware, {'allowed_hosts': ('good.test:80',)}),
                                 (ProxyHeadersMiddleware, {'trusted_proxies': ('*',)}),
                                 (SecurityHeadersMiddleware, {'headers': {'set-cookie': 'x=y'}}),
                                 (SecurityHeadersMiddleware, {'headers': {'x-custom': 'injected\nvalue'}}),
                                 (CORSMiddleware, {'origins': ('*',), 'credentials': True}),
                                 (CORSMiddleware, {'origins': ('https://good.test/',)}),
                                 (CORSMiddleware, {'origins': ('https://good.test',), 'headers': ('*',)}),
                                 (CORSMiddleware, {'origins': ('https://good.test',), 'headers': (1,)}),
                                 (CSRFMiddleware, {'origins': ('https://good.test',), 'token': None}),
                                 (RateLimitMiddleware, {'decide': None})):
            with self.assertRaises(ConfigurationError):
                factory(App(), **options)
