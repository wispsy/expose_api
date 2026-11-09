import tempfile
import unittest
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import patch

from expose_api import App, ConfigurationError, JSONResponse, ProtocolError, Response, Router, StaticFiles
from asgi_client import request


class StaticTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.work = tempfile.TemporaryDirectory()
        self.addCleanup(self.work.cleanup)
        self.root = Path(self.work.name) / 'assets'
        self.root.mkdir()
        (self.root / 'css').mkdir()
        (self.root / 'css' / 'site.css').write_bytes(b'body{color:blue}')
        (self.root / 'café.css').write_bytes(b'unicode')
        (self.root / '.private').write_bytes(b'hidden')
        self.outside = Path(self.work.name) / 'outside.txt'
        self.outside.write_bytes(b'private outside bytes')

    def app(self):
        app = App()
        app.mount('/static', StaticFiles(self.root), name='assets')
        return app

    async def test_mime_cache_head_conditions_and_ranges(self):
        app = self.app()
        result = await request(app, '/static/css/site.css', hold=True)
        self.assertEqual((result.status, result.body), (200, b'body{color:blue}'))
        self.assertTrue(result.headers[b'content-type'].startswith(b'text/css'))
        self.assertEqual(result.headers[b'x-content-type-options'], b'nosniff')
        self.assertIn(b'must-revalidate', result.headers[b'cache-control'])
        cached = await request(app, '/static/css/site.css', headers=[(b'if-none-match', result.headers[b'etag'])])
        self.assertEqual(cached.status, 304)
        repeated = await request(app, '/static/css/site.css',
                                 headers=[(b'if-none-match', b'"old"'), (b'if-none-match', result.headers[b'etag'])])
        self.assertEqual(repeated.status, 304)
        partial = await request(app, '/static/css/site.css', headers=[(b'range', b'bytes=0-3')], hold=True)
        self.assertEqual((partial.status, partial.body), (206, b'body'))
        head = await request(app, '/static/css/site.css', 'HEAD')
        self.assertEqual((head.status, head.body, head.headers[b'content-length']), (200, b'', b'16'))

    async def test_named_mount_urls_root_paths_and_single_decoding(self):
        app = self.app()
        app.route('/page', public=True)(lambda r: JSONResponse({'asset': r.url_for('assets', path='café.css')}))
        result = await request(app, '/base/page', root_path='/base')
        self.assertEqual(result.json, {'asset': '/base/static/caf%C3%A9.css'})
        self.assertEqual((await request(app, '/base/static/café.css', root_path='/base', hold=True)).body, b'unicode')
        (self.root / '%2e%2e.css').write_bytes(b'literal percent filename')
        self.assertEqual((await request(app, '/static/%2e%2e.css', hold=True)).body, b'literal percent filename')
        self.assertEqual(app.url_path_for('assets', path='css/site.css'), '/static/css/site.css')
        with self.assertRaises(ConfigurationError):
            app.url_path_for('assets', path='../outside.txt')

    async def test_traversal_portable_names_hidden_files_and_directories(self):
        app = self.app()
        for tail in ('../outside.txt', 'css/../../outside.txt', 'css\\site.css', 'css//site.css',
                     '.private', 'CON', 'CON .txt', 'COM¹.txt', 'site.css:stream', 'css/site.css.',
                     'css/', '', 'missing', 'nul\x00name'):
            with self.subTest(tail=tail):
                result = await request(app, '/static/' + tail)
                self.assertEqual(result.status, 404)
                self.assertNotIn(b'private outside bytes', result.body)
                self.assertNotIn(b'location', result.headers)

    async def test_symlinks_and_portable_fallback(self):
        try:
            (self.root / 'escape.txt').symlink_to(self.outside)
            (self.root / 'escape-dir').symlink_to(Path(self.work.name), target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest('This runner cannot create symbolic links.')
        app = self.app()
        for portable in (False, True):
            manager = patch('expose_api.static.os.supports_dir_fd', set()) if portable else nullcontext()
            with manager:
                for tail in ('escape.txt', 'escape-dir/outside.txt'):
                    self.assertEqual((await request(app, '/static/' + tail)).status, 404)
                self.assertEqual((await request(app, '/static/css/site.css', hold=True)).status, 200)

    async def test_methods_and_explicit_hidden_file_policy(self):
        app = self.app()
        for method, status in (('OPTIONS', 204), ('POST', 405)):
            result = await request(app, '/static/css/site.css', method)
            self.assertEqual((result.status, result.headers[b'allow'], result.reads), (status, b'GET, HEAD, OPTIONS', 0))
        selected = App()
        selected.mount('/chosen', StaticFiles(self.root, allow_hidden=True))
        self.assertEqual((await request(selected, '/chosen/.private', hold=True)).body, b'hidden')
        with self.assertRaises(ProtocolError):
            await StaticFiles(self.root)({'type': 'lifespan'}, None, None)

    async def test_name_collisions_and_freeze_are_atomic(self):
        app = self.app()
        with self.assertRaises(ConfigurationError):
            app.route('/collision', public=True, name='assets')(lambda r: Response())
        router = Router()
        router.route('/another', public=True, name='assets')(lambda r: Response())
        with self.assertRaises(ConfigurationError):
            app.include_router(router)
        self.assertEqual((await request(app, '/another')).status, 404)
        with self.assertRaises(ConfigurationError):
            app.mount('/late', StaticFiles(self.root), name='late')
        other = App()
        other.route('/page', public=True, name='page')(lambda r: Response())
        with self.assertRaises(ConfigurationError):
            other.mount('/static', StaticFiles(self.root), name='page')
        for directory in (self.root / 'missing', self.outside):
            with self.assertRaises(ConfigurationError):
                StaticFiles(directory)
