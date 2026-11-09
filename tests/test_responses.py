import math
import unittest

from expose_api import App, ConfigurationError, HTMLResponse, HTTPException, JSONResponse, RedirectResponse, Response
from asgi_client import request


class ResponseTests(unittest.IsolatedAsyncioTestCase):
    async def test_wire_bytes_and_repeated_headers(self):
        response = JSONResponse({'title': 'café'}, status=201, headers=[('x-tag', 'one'), ('X-Tag', 'two')])
        result = await request(response, '/')
        self.assertEqual((result.status, result.json), (201, {'title': 'café'}))
        self.assertEqual(result.headers[b'content-length'], str(len(result.body)).encode())
        self.assertEqual(response.headers[:2], [(b'x-tag', b'one'), (b'x-tag', b'two')])
        self.assertEqual((await request(response, '/', 'HEAD')).body, b'')

    async def test_empty_html_and_redirect_responses(self):
        self.assertEqual((await request(Response(status=204))).headers, {})
        self.assertEqual((await request(Response(status=304))).body, b'')
        self.assertEqual((await request(HTMLResponse('<p>Hi</p>'))).headers[b'content-type'], b'text/html; charset=utf-8')
        result = await request(RedirectResponse('/café?q=one%20two', status=303))
        self.assertEqual((result.status, result.headers[b'location']), (303, b'/caf%C3%A9?q=one%20two'))

    def test_invalid_headers_framing_status_and_json(self):
        for headers in ([('x-test', 'bad\r\nx-injected: yes')], [('bad name', 'x')], [('x', '\x00')],
                        [('content-length', '3')], [('transfer-encoding', 'chunked')], [('content-length', '0')] * 2):
            with self.subTest(headers=headers), self.assertRaises(ConfigurationError):
                Response(headers=headers)
        for status in (True, 100, 600):
            with self.assertRaises(ConfigurationError):
                Response(status=status)
        with self.assertRaises(ConfigurationError):
            Response('forbidden', status=204)
        with self.assertRaises(ValueError):
            JSONResponse({'value': math.nan})
        with self.assertRaises(ConfigurationError):
            RedirectResponse('/bad\nheader')
        self.assertEqual(HTTPException(418, 'Public', headers={'x-reason': 'tea'}).detail, 'Public')

    def test_cookie_security_and_deletion(self):
        response = Response()
        response.set_cookie('__Host-session', 'quoted value', secure=True, httponly=True)
        response.delete_cookie('old')
        cookies = [v for k, v in response.headers if k == b'set-cookie']
        self.assertEqual(len(cookies), 2)
        for attribute in (b'HttpOnly', b'Secure', b'SameSite=lax', b'Path=/'):
            self.assertIn(attribute, cookies[0])
        self.assertIn(b'Max-Age=0', cookies[1])
        for options in ({'name': '__Host-session'}, {'name': 'bad\r\n'}, {'name': 'expires'},
                        {'name': 'ok', 'path': '/;unsafe'}, {'name': 'ok', 'value': 'secret\n'},
                        {'name': 'ok', 'samesite': 'none'}):
            with self.subTest(options=options), self.assertRaises(ConfigurationError):
                response.set_cookie(**options)

    async def test_transport_failure_is_not_retried(self):
        messages = []
        async def send(event):
            messages.append(event)
            raise OSError('closed')
        with self.assertRaises(OSError):
            await Response(b'body')({'method': 'GET'}, None, send)
        self.assertEqual(len(messages), 1)

    async def test_mutated_responses_fail_before_start_and_are_sanitized_in_app(self):
        mutations = ({'headers': [(b'x-private', b'private\r\ninjected: yes')]},
                     {'headers': None}, {'headers': [(b'x-incomplete',)]},
                     {'headers': [(b'content-length', b'99')]},
                     {'headers': [(b'content-length', b'1')] * 2},
                     {'headers': [(b'transfer-encoding', b'chunked')]},
                     {'body': b'changed'}, {'body': 'text'}, {'status': 204}, {'status': True})
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                response = Response(b'x')
                for name, value in mutation.items():
                    setattr(response, name, value)
                messages = []
                async def send(event):
                    messages.append(event)
                with self.assertRaises(ConfigurationError):
                    await response({'method': 'GET'}, None, send)
                self.assertEqual(messages, [])
                app = App()
                app.route('/bad', public=True)(lambda r: response)
                with self.assertLogs('expose_api', 'ERROR'):
                    result = await request(app, '/bad')
                self.assertEqual((result.status, result.json['error']['code']), (500, 'internal'))
                self.assertNotIn(b'private', result.body)
        valid = Response(b'x')
        valid.headers = [(b'X-Tag', b'one'), (b'x-tag', b'two')]
        valid.set_cookie('session', 'value')
        result = await request(valid)
        self.assertEqual((result.body, result.headers[b'content-length']), (b'x', b'1'))
        self.assertEqual(result.messages[0]['headers'][:2], [(b'x-tag', b'one'), (b'x-tag', b'two')])

    async def test_sending_uses_the_validated_body_snapshot(self):
        response = Response(b'original')
        messages = []
        async def send(event):
            messages.append(event)
            if event['type'] == 'http.response.start':
                response.body = b'changed after headers'
        await response({'method': 'GET'}, None, send)
        self.assertEqual(messages[1]['body'], b'original')
