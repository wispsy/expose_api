import asyncio
import tempfile
import threading
import unittest
from unittest.mock import patch

from expose_api import App, ConfigurationError, HTTPRequest, JSONResponse, MultipartLimits, UploadFile
from expose_api.http_protocol import _HTTPError
from asgi_client import request


def form_body(parts, boundary=b'Boundary'):
    return b''.join(b'--' + boundary + b'\r\n' + headers + b'\r\n\r\n' + data + b'\r\n'
                    for headers, data in parts) + b'--' + boundary + b'--\r\n'


FIELD = b'Content-Disposition: form-data; name="tag"'
FILE = b'Content-Disposition: form-data; name="upload"; filename="../../caf\xc3\xa9.bin"\r\nContent-Type: application/octet-stream'
HEADERS = [(b'content-type', b'multipart/form-data; boundary=Boundary')]


class MultipartTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.spooled = []
        factory = tempfile.SpooledTemporaryFile
        def tracked(*args, **kwargs):
            file = factory(*args, **kwargs)
            self.spooled.append(file)
            return file
        self.patcher = patch('expose_api.multipart.tempfile.SpooledTemporaryFile', tracked)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)

    def app(self, *, limits=None, fail=False):
        app = App()
        @app.route('/upload', methods=('POST',), public=True)
        async def upload(context):
            async with context.multipart(limits=limits) as form:
                files = []
                for name, value in form.multi_items():
                    if isinstance(value, UploadFile):
                        files.append({'name': name, 'filename': value.filename, 'type': value.content_type,
                                      'size': value.size, 'data': (await value.read()).hex()})
                        await value.seek(0)
                        self.assertEqual(len(await value.read()), value.size)
                if fail:
                    raise RuntimeError('private')
                return JSONResponse({'tags': form.getlist('tag'), 'files': files})
        return app

    async def test_duplicate_fields_binary_files_spooling_and_owned_lifetime(self):
        payload = b'\x00\xff\r\n--BoundaryX\r\nbytes'
        body = form_body([(FIELD, b'caf\xc3\xa9'), (FIELD, b'two'), (FILE, payload), (FILE, b'')])
        result = await request(self.app(limits=MultipartLimits(spool_threshold=1)), '/upload', 'POST', body=body, headers=HEADERS)
        self.assertEqual(result.status, 200)
        self.assertEqual(result.json['tags'], ['café', 'two'])
        self.assertEqual(result.json['files'][0], {'name': 'upload', 'filename': '../../café.bin',
                                                 'type': 'application/octet-stream', 'size': len(payload), 'data': payload.hex()})
        self.assertEqual(len(result.json['files']), 2)
        self.assertTrue(any(getattr(file, '_rolled') for file in self.spooled))
        self.assertTrue(all(file.closed for file in self.spooled))

    async def test_total_actual_and_declared_limits_before_spooling(self):
        body = form_body([(FILE, b'content')])
        app = self.app(limits=MultipartLimits(max_total_bytes=16))
        result = await request(app, '/upload', 'POST', body=body,
                               headers=[*HEADERS, (b'content-length', str(len(body)).encode())])
        self.assertEqual((result.status, result.reads), (413, 0))
        result = await request(app, '/upload', 'POST', headers=HEADERS, events=[
            {'type': 'http.request', 'body': body[:10], 'more_body': True},
            {'type': 'http.request', 'body': body[10:]}])
        self.assertEqual(result.status, 413)
        self.assertEqual(self.spooled, [])

    async def test_part_field_header_and_duplicate_count_limits(self):
        for parts, limits in (([(FIELD, b'one'), (FIELD, b'two')], MultipartLimits(max_parts=1)),
                              ([(FIELD, b'one'), (FIELD, b'two')], MultipartLimits(max_fields=1)),
                              ([(FILE, b'one'), (FILE, b'two')], MultipartLimits(max_files=1)),
                              ([(FIELD, b'long')], MultipartLimits(max_field_bytes=2)),
                              ([(FILE, b'long')], MultipartLimits(max_part_bytes=2)),
                              ([(FILE, b'x')], MultipartLimits(max_header_bytes=40))):
            result = await request(self.app(limits=limits), '/upload', 'POST', body=form_body(parts), headers=HEADERS)
            self.assertEqual(result.status, 413)
            self.assertTrue(all(file.closed for file in self.spooled))

    async def test_malformed_headers_encodings_boundaries_and_nested_mime(self):
        invalid = [form_body([(FIELD + b'\r\nContent-Disposition: form-data; name="other"', b'x')]),
                   form_body([(FIELD + b'\r\nContent-Transfer-Encoding: base64', b'eA==')]),
                   form_body([(FILE.replace(b'application/octet-stream', b'multipart/mixed'), b'x')]),
                   form_body([(FIELD, b'\xff')]), form_body([(FIELD + b'; name="again"', b'x')]),
                   form_body([(FIELD + b'; filename*=UTF-8\'\'name', b'x')]),
                   form_body([(FIELD + b'\r\nContent-Type: text/plain; charset=latin-1', b'x')]),
                   b'preamble\r\n' + form_body([(FIELD, b'x')]), form_body([(FIELD, b'x')]) + b'epilogue',
                   form_body([(FIELD, b'x')]).replace(b'--Boundary--', b'--Wrong--')]
        for body in invalid:
            with self.subTest(body=body[:60]):
                result = await request(self.app(), '/upload', 'POST', body=body, headers=HEADERS)
                self.assertEqual(result.status, 400)
        for content_type in (b'multipart/form-data', b'multipart/form-data; boundary=Boundary; boundary=Other',
                             b'multipart/form-data; boundary="space boundary"'):
            result = await request(self.app(), '/upload', 'POST', headers=[(b'content-type', content_type)])
            self.assertEqual((result.status, result.reads), (400, 0))

    async def test_partial_parse_and_handler_errors_close_all_files(self):
        result = await request(self.app(), '/upload', 'POST', body=form_body([(FILE, b'first'), (FIELD, b'\xff')]), headers=HEADERS)
        self.assertEqual(result.status, 400)
        self.assertTrue(self.spooled and all(file.closed for file in self.spooled))
        with self.assertLogs('expose_api', 'ERROR'):
            result = await request(self.app(fail=True), '/upload', 'POST', body=form_body([(FILE, b'first')]), headers=HEADERS)
        self.assertEqual(result.status, 500)
        self.assertTrue(all(file.closed for file in self.spooled))

    async def test_cancellation_while_spooling_closes_the_created_file(self):
        import importlib
        module = importlib.import_module('expose_api.multipart')
        original_io = module._io
        started = asyncio.Event()
        async def interrupted(function, *args):
            if function.__name__ == 'write':
                started.set()
                await asyncio.Future()
            return await original_io(function, *args)
        async def operation():
            await request(self.app(), '/upload', 'POST', body=form_body([(FILE, b'first')]), headers=HEADERS)
        with patch('expose_api.multipart._io', interrupted):
            task = asyncio.create_task(operation())
            await started.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertTrue(self.spooled and all(file.closed for file in self.spooled))

    async def test_repeated_cancellation_finishes_spooling_before_closing(self):
        started, release, finished = threading.Event(), threading.Event(), threading.Event()
        cleanup = []
        factory = tempfile.SpooledTemporaryFile
        class DelayedWrite:
            def __init__(self, file):
                self.file = file
            def __getattr__(self, name):
                return getattr(self.file, name)
            def write(self, data):
                started.set()
                release.wait(10)
                try:
                    return self.file.write(data)
                finally:
                    finished.set()
            def close(self):
                cleanup.append(finished.is_set())
                return self.file.close()
        def delayed(*args, **kwargs):
            return DelayedWrite(factory(*args, **kwargs))
        with patch('expose_api.multipart.tempfile.SpooledTemporaryFile', delayed):
            task = asyncio.create_task(request(self.app(), '/upload', 'POST',
                                               body=form_body([(FILE, b'content')]), headers=HEADERS))
            try:
                self.assertTrue(await asyncio.to_thread(started.wait, 5))
                for _ in range(3):
                    task.cancel()
                    await asyncio.sleep(0)
                    await asyncio.sleep(0)
                    self.assertFalse(task.done())
                    self.assertFalse(self.spooled[-1].closed)
                release.set()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                self.assertEqual(cleanup, [True])
                self.assertTrue(all(file.closed for file in self.spooled))
            finally:
                release.set()
                await asyncio.gather(task, return_exceptions=True)
                await asyncio.to_thread(finished.wait, 5)
                for file in self.spooled:
                    file.close()

    async def test_authorization_and_media_fail_before_body(self):
        app = self.app()
        result = await request(app, '/upload', 'POST', headers=[(b'content-type', b'application/json')])
        self.assertEqual((result.status, result.reads), (415, 0))
        denied = App()
        @denied.route('/upload', methods=('POST',), authorize=lambda r: False)
        async def upload(context):
            async with context.multipart():
                self.fail('denied request reached parser')
        result = await request(denied, '/upload', 'POST', headers=HEADERS)
        self.assertEqual((result.status, result.reads, result.headers[b'cache-control']), (403, 0, b'no-store'))

    async def test_cached_body_cannot_bypass_lower_limit_and_limit_configuration(self):
        async def receive():
            return {'type': 'http.request', 'body': b'long'}
        context = HTTPRequest({'method': 'POST', 'path': '/'}, receive)
        self.assertEqual(await context.body(), b'long')
        with self.assertRaises(_HTTPError) as caught:
            await context.body(max_size=2)
        self.assertEqual(caught.exception.status, 413)
        for options in ({'max_total_bytes': 65 * 1024 * 1024}, {'max_files': True}, {'spool_threshold': 0}):
            with self.assertRaises(ConfigurationError):
                MultipartLimits(**options)
