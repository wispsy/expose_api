import asyncio
import unittest

from expose_api import HTTPRequest, ProtocolError, Request
from expose_api.http_protocol import _Disconnected, _HTTPError


def reader(*, body=b'', events=None, headers=(), query=b'', limit=1024):
    pending = list(events) if events is not None else [{'type': 'http.request', 'body': body}]
    reads = []
    async def receive():
        reads.append(True)
        return pending.pop(0)
    scope = {'type': 'http', 'path': '/echo', 'method': 'POST', 'headers': headers,
             'query_string': query, 'state': {'initial': True}}
    return HTTPRequest(scope, receive, max_body_size=limit), reads, scope


class RequestTests(unittest.IsolatedAsyncioTestCase):
    async def test_body_is_cached_and_json_uses_the_same_bytes(self):
        request, reads, scope = reader(events=[{'type': 'http.request', 'body': b'{"x":', 'more_body': True},
                                               {'type': 'http.request', 'body': b'1}'}],
                                       headers=[(b'content-type', b'application/example+json'), (b'content-length', b'7')])
        self.assertEqual(await request.json(), {'x': 1})
        self.assertEqual(await request.body(), b'{"x":1}')
        self.assertEqual(await request.json(), {'x': 1})
        self.assertEqual(len(reads), 2)
        request.state['local'] = True
        self.assertNotIn('local', scope['state'])
        self.assertIsNot(Request, HTTPRequest)

    async def test_repeated_query_headers_forms_and_cookies(self):
        request, _, _ = reader(query=b'q=caf%C3%A9&q=two&empty=', body=b'a=one+two&a=three',
                               headers=[(b'content-type', b'application/x-www-form-urlencoded; charset=UTF-8'),
                                        (b'x-value', b'one'), (b'X-Value', b'two'), (b'cookie', b'session="value"')])
        self.assertEqual(request.query_params.getlist('q'), ['café', 'two'])
        self.assertEqual(request.query_params['empty'], '')
        self.assertEqual(request.headers.getlist('x-value'), ['one', 'two'])
        self.assertIsNone(request.header('X-Value'))
        self.assertEqual(request.cookies, {'session': 'value'})
        self.assertEqual((await request.form()).getlist('a'), ['one two', 'three'])

    async def test_limits_and_lengths_remain_failed_without_rereading(self):
        cases = [({'body': b'12345', 'limit': 4}, 413),
                 ({'body': b'12', 'headers': [(b'content-length', b'3')]}, 400),
                 ({'headers': [(b'content-length', b'1'), (b'content-length', b'1')]}, 400),
                 ({'headers': [(b'content-length', b'99')], 'limit': 4}, 413),
                 ({'events': [{'type': 'http.request', 'body': b'123', 'more_body': True},
                              {'type': 'http.request', 'body': b'45'}], 'limit': 4}, 413)]
        for options, status in cases:
            request, reads, _ = reader(**options)
            with self.subTest(options=options):
                with self.assertRaises(_HTTPError) as caught:
                    await request.body()
                self.assertEqual(caught.exception.status, status)
                count = len(reads)
                with self.assertRaises(_HTTPError):
                    await request.body()
                self.assertEqual(len(reads), count)

    async def test_media_and_malformed_values(self):
        for body in (b'{"x":1,"x":2}', b'{"x":NaN}', b'\xff', b'[' * 40 + b'0' + b']' * 40):
            request, _, _ = reader(body=body, headers=[(b'content-type', b'application/json')])
            with self.assertRaises(_HTTPError) as caught:
                await request.json()
            self.assertEqual(caught.exception.code, 'json')
        for headers in ([], [(b'content-type', b'text/plain')],
                        [(b'content-type', b'application/json; charset = latin-1')],
                        [(b'content-type', b'application/json')] * 2):
            request, reads, _ = reader(headers=headers)
            with self.assertRaises(_HTTPError) as caught:
                await request.json()
            self.assertEqual((caught.exception.status, len(reads)), (415, 0))
        for query in (b'q=%XX', b'q=%FF', b'&'.join([b'a=b'] * 129), b'a=' + b'x' * 8192):
            request, _, _ = reader(query=query)
            with self.assertRaises(_HTTPError):
                request.query_params
        request, _, _ = reader(body=b'q=%FF', headers=[(b'content-type', b'application/x-www-form-urlencoded')])
        with self.assertRaises(_HTTPError):
            await request.form()

    async def test_disconnect_bad_events_cancellation_and_concurrent_reader(self):
        for event, exception in (({'type': 'http.disconnect'}, _Disconnected),
                                 ({'type': 'http.request', 'body': 'text'}, ProtocolError),
                                 ({'type': 'http.request', 'more_body': 1}, ProtocolError)):
            request, _, _ = reader(events=[event])
            with self.assertRaises(exception):
                await request.body()
        started = asyncio.Event()
        async def receive():
            started.set()
            await asyncio.Future()
        request = HTTPRequest({'method': 'POST', 'path': '/'}, receive)
        task = asyncio.create_task(request.body())
        await started.wait()
        with self.assertRaises(ProtocolError):
            await request.body()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        with self.assertRaises(asyncio.CancelledError):
            await request.body()

    async def test_malformed_event_shapes_remain_protocol_failures_without_rereading(self):
        for event in (None, [], {}, {'body': b'private'}, {'type': None}, {'type': 'websocket.receive'},
                      {'type': 'http.request', 'body': bytearray()},
                      {'type': 'http.request', 'more_body': None}):
            with self.subTest(event=event):
                context, reads, _ = reader(events=[event])
                with self.assertRaises(ProtocolError):
                    await context.body()
                with self.assertRaises(ProtocolError):
                    await context.body()
                self.assertEqual(len(reads), 1)
        context, reads, _ = reader(events=[{'type': 'http.request', 'extra': 'ignored'}])
        self.assertEqual(await context.body(), b'')
        self.assertEqual(len(reads), 1)
