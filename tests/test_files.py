import asyncio
import tempfile
import threading
import unittest
from pathlib import Path

from expose_api import App, ConfigurationError, FileResponse, ProtocolError
from expose_api.files import _io
from asgi_client import request


class FileTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / 'sample.txt'
        self.path.write_bytes(b'0123456789')
        self.opened = []

    def opener(self, path):
        file = open(path, 'rb')
        self.opened.append(file)
        return file

    def app(self):
        app = App()
        app.route('/file', public=True)(lambda r: FileResponse(self.path, _opener=self.opener))
        return app

    async def test_full_file_mime_download_and_head_close_descriptors(self):
        result = await request(FileResponse(self.path, filename='café.txt', chunk_size=3, _opener=self.opener), hold=True)
        self.assertEqual((result.status, result.body, result.headers[b'content-length']), (200, b'0123456789', b'10'))
        self.assertTrue(result.headers[b'content-type'].startswith(b'text/plain'))
        self.assertIn(b"filename*=UTF-8''caf%C3%A9.txt", result.headers[b'content-disposition'])
        self.assertTrue(all(len(event.get('body', b'')) <= 3 for event in result.messages[1:]))
        head = await request(self.app(), '/file', 'HEAD')
        self.assertEqual((head.status, head.body, head.headers[b'content-length']), (200, b'', b'10'))
        self.assertTrue(all(file.closed for file in self.opened))

    async def test_conditional_precedence_lists_and_dates(self):
        app = self.app()
        first = await request(app, '/file', hold=True)
        etag, modified = first.headers[b'etag'], first.headers[b'last-modified']
        for headers in ([(b'if-none-match', etag)], [(b'if-none-match', b'"unrelated,tag", ' + etag.removeprefix(b'W/'))],
                        [(b'if-none-match', b'*')], [(b'if-modified-since', modified)]):
            result = await request(app, '/file', headers=headers, hold=True)
            self.assertEqual((result.status, result.body), (304, b''))
            self.assertNotIn(b'content-length', result.headers)
        for headers in ([(b'if-none-match', b'"different"'), (b'if-modified-since', modified)],
                        [(b'if-none-match', b'invalid')], [(b'if-modified-since', b'not a date')]):
            self.assertEqual((await request(app, '/file', headers=headers, hold=True)).status, 200)
        self.assertTrue(all(file.closed for file in self.opened))

    async def test_single_ranges_suffix_clamping_head_and_if_range(self):
        app = self.app()
        for value, body, content_range in ((b'bytes=2-4', b'234', b'bytes 2-4/10'),
                                            (b'bytes=7-', b'789', b'bytes 7-9/10'),
                                            (b'bytes=-3', b'789', b'bytes 7-9/10'),
                                            (b'bytes=8-99', b'89', b'bytes 8-9/10')):
            result = await request(app, '/file', headers=[(b'range', value)], hold=True)
            self.assertEqual((result.status, result.body, result.headers[b'content-range']), (206, body, content_range))
            self.assertEqual(int(result.headers[b'content-length']), len(body))
        for value in (b'bytes=10-', b'bytes=4-1', b'bytes=-0', b'bytes=0-1,3-4', b'bytes=' + b'9' * 100 + b'-'):
            result = await request(app, '/file', headers=[(b'range', value)], hold=True)
            self.assertEqual((result.status, result.headers[b'content-range']), (416, b'bytes */10'))
        first = await request(app, '/file', hold=True)
        for if_range, status in ((first.headers[b'etag'], 200), (b'Thu, 01 Jan 1970 00:00:00 GMT', 200),
                                 (first.headers[b'last-modified'], 206)):
            result = await request(app, '/file', headers=[(b'range', b'bytes=0-1'), (b'if-range', if_range)], hold=True)
            self.assertEqual(result.status, status)
        head = await request(app, '/file', 'HEAD', headers=[(b'range', b'bytes=0-1')])
        self.assertEqual((head.status, head.headers[b'content-length']), (200, b'10'))
        self.assertEqual((await request(app, '/file', headers=[(b'range', b'items=1-2')], hold=True)).status, 200)

    async def test_repeated_none_match_fields_keep_precedence_and_aggregate_limits(self):
        app = self.app()
        first = await request(app, '/file', hold=True)
        etag, modified = first.headers[b'etag'], first.headers[b'last-modified']
        for method in ('GET', 'HEAD'):
            result = await request(app, '/file', method, hold=True,
                                   headers=[(b'if-none-match', b'"other"'), (b'If-None-Match', etag)])
            self.assertEqual((result.status, result.body), (304, b''))
            self.assertNotIn(b'content-length', result.headers)
        cases = [(b'"one"', b'"two"'), (b'invalid', etag), (b'*', etag),
                 (b'"' + b'x' * 4096 + b'"', etag + b', "' + b'y' * 4096 + b'"'),
                 (b', '.join([b'"old"'] * 128), etag)]
        for values in cases:
            with self.subTest(values=values):
                result = await request(app, '/file', hold=True,
                                       headers=[*((b'if-none-match', value) for value in values),
                                                (b'if-modified-since', modified)])
                self.assertEqual((result.status, result.body), (200, b'0123456789'))
        duplicate_dates = await request(app, '/file', hold=True,
                                        headers=[(b'if-modified-since', modified)] * 2)
        self.assertEqual(duplicate_dates.status, 200)
        self.assertTrue(all(file.closed for file in self.opened))

    async def test_empty_missing_and_preparation_errors(self):
        self.path.write_bytes(b'')
        self.assertEqual((await request(self.app(), '/file', hold=True)).body, b'')
        self.assertEqual((await request(self.app(), '/file', headers=[(b'range', b'bytes=0-')], hold=True)).status, 416)
        self.path.unlink()
        result = await request(self.app(), '/file')
        self.assertEqual(result.status, 404)
        self.assertNotIn(str(self.path), result.body.decode())
        app = App()
        app.route('/directory', public=True)(lambda r: FileResponse(self.directory.name))
        self.assertEqual((await request(app, '/directory')).status, 404)

    async def test_failed_send_and_changed_file_close_descriptors(self):
        prepared = await FileResponse(self.path, _opener=self.opener).prepare({'method': 'GET'})
        self.path.write_bytes(b'')
        async def receive():
            await asyncio.Future()
        events = []
        async def send(event):
            events.append(event)
        with self.assertRaises(ProtocolError):
            await prepared({'method': 'GET'}, receive, send)
        self.assertEqual(sum(event['type'] == 'http.response.start' for event in events), 1)
        self.assertTrue(self.opened[-1].closed)
        async def failed_send(event):
            raise OSError('closed')
        with self.assertRaises(OSError):
            await FileResponse(self.path, _opener=self.opener)({'method': 'GET'}, receive, failed_send)
        self.assertTrue(self.opened[-1].closed)

    async def test_canceled_open_reconciles_thread_and_closes_file(self):
        started, release = threading.Event(), threading.Event()
        def delayed(path):
            file = self.opener(path)
            started.set()
            release.wait(3)
            return file
        task = asyncio.create_task(FileResponse(self.path, _opener=delayed).prepare({'method': 'GET'}))
        self.assertTrue(await asyncio.to_thread(started.wait, 2))
        task.cancel()
        release.set()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(self.opened[-1].closed)

    async def test_repeated_cancellation_keeps_open_owned_until_the_worker_returns(self):
        started, release, finished = threading.Event(), threading.Event(), threading.Event()
        def delayed(path):
            file = self.opener(path)
            started.set()
            release.wait(10)
            finished.set()
            return file
        task = asyncio.create_task(FileResponse(self.path, _opener=delayed).prepare({'method': 'GET'}))
        try:
            self.assertTrue(await asyncio.to_thread(started.wait, 5))
            for _ in range(3):
                task.cancel()
                await asyncio.sleep(0)
                await asyncio.sleep(0)
                self.assertFalse(task.done())
                self.assertFalse(self.opened[-1].closed)
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertTrue(finished.is_set())
            self.assertTrue(self.opened[-1].closed)
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)
            await asyncio.to_thread(finished.wait, 5)
            for file in self.opened:
                file.close()

    async def test_repeated_cancellation_finishes_io_before_caller_cleanup(self):
        loop, errors = asyncio.get_running_loop(), []
        self.addCleanup(loop.set_exception_handler, loop.get_exception_handler())
        loop.set_exception_handler(lambda loop, context: errors.append(context))
        for fail in (False, True):
            with self.subTest(fail=fail):
                started, release, finished = threading.Event(), threading.Event(), threading.Event()
                file, cleanup = self.opener(self.path), []
                def delayed_read():
                    started.set()
                    release.wait(10)
                    try:
                        if fail:
                            raise OSError('worker failure after cancellation')
                        return file.read()
                    finally:
                        finished.set()
                async def operation():
                    try:
                        await _io(delayed_read)
                    finally:
                        cleanup.append(finished.is_set())
                        file.close()
                task = asyncio.create_task(operation())
                try:
                    self.assertTrue(await asyncio.to_thread(started.wait, 5))
                    for _ in range(3):
                        task.cancel()
                        await asyncio.sleep(0)
                        await asyncio.sleep(0)
                        self.assertFalse(task.done())
                        self.assertFalse(file.closed)
                    release.set()
                    with self.assertRaises(asyncio.CancelledError):
                        await task
                    self.assertEqual(cleanup, [True])
                    self.assertTrue(file.closed)
                finally:
                    release.set()
                    await asyncio.gather(task, return_exceptions=True)
                    await asyncio.to_thread(finished.wait, 5)
                    file.close()
        self.assertEqual(errors, [])

    async def test_stream_cleanup_closes_file_after_repeated_cancellation(self):
        started, release, finished = threading.Event(), threading.Event(), threading.Event()
        file, cleanup = self.opener(self.path), []
        class HeldRead:
            def __getattr__(self, name):
                return getattr(file, name)
            def read(self, size):
                started.set()
                release.wait(10)
                try:
                    return file.read(size)
                finally:
                    finished.set()
            def close(self):
                cleanup.append(finished.is_set())
                file.close()
        response = await FileResponse(self.path, _opener=lambda path: HeldRead()).prepare({'method': 'GET'})
        async def receive():
            await asyncio.Future()
        async def send(event):
            pass
        task = asyncio.create_task(response({'method': 'GET'}, receive, send))
        try:
            self.assertTrue(await asyncio.to_thread(started.wait, 5))
            for _ in range(3):
                task.cancel()
                await asyncio.sleep(0)
                await asyncio.sleep(0)
                self.assertFalse(task.done())
                self.assertFalse(file.closed)
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertEqual(cleanup, [True])
            self.assertTrue(file.closed)
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)
            await asyncio.to_thread(finished.wait, 5)
            file.close()

    def test_configuration(self):
        for options in ({'filename': '../bad'}, {'filename': 'bad\nname'}, {'headers': {'etag': 'caller'}}, {'chunk_size': 0}):
            with self.assertRaises(ConfigurationError):
                FileResponse(self.path, **options)
