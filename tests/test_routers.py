import unittest
from uuid import UUID

from expose_api import App, ConfigurationError, JSONResponse, Response, Router
from asgi_client import request


class RouterTests(unittest.IsolatedAsyncioTestCase):
    async def test_nested_prefixes_snapshot_and_named_urls(self):
        child = Router(prefix='/items')
        @child.route('/{name}', public=True, name='item')
        def item(context):
            return JSONResponse({'url': context.url_for('v1:item', name=context.path_params['name'])})
        parent = Router(prefix='/api')
        parent.include_router(child, prefix='/v1', name_prefix='v1:')
        app = App()
        app.include_router(parent, prefix='/service')
        child.route('/later', public=True)(lambda r: Response())
        result = await request(app, '/root/service/api/v1/items/café', root_path='/root',
                               headers=[(b'host', b'untrusted.example')])
        self.assertEqual(result.json, {'url': '/root/service/api/v1/items/caf%C3%A9'})
        self.assertEqual(app.url_path_for('v1:item', name='one%20two'), '/service/api/v1/items/one%2520two')
        self.assertEqual(len(app._router._routes), 1)
        with self.assertRaises(ConfigurationError):
            app.include_router(child)

    async def test_repeated_inclusion_and_atomic_errors(self):
        router = Router()
        router.route('/x', public=True, name='x')(lambda r: Response(b'x'))
        app = App()
        app.include_router(router, prefix='/one', name_prefix='one:')
        app.include_router(router, prefix='/two', name_prefix='two:')
        self.assertEqual(app.url_path_for('two:x'), '/two/x')
        colliding = Router()
        colliding.route('/new', public=True)(lambda r: Response())
        colliding.route('/one/x', public=True)(lambda r: Response())
        with self.assertRaises(ConfigurationError):
            app.include_router(colliding)
        self.assertEqual(len(app._router._routes), 2)
        self.assertEqual((await request(app, '/new')).status, 404)
        self.assertEqual((await request(app, '/two/x')).body, b'x')

    def test_url_validation_and_converters(self):
        app = App()
        app.route('/files/{tail:path}', public=True, name='file')(lambda r: Response())
        app.route('/ids/{identity:uuid}/{number:int}', public=True, name='id')(lambda r: Response())
        app.route('/reserved/{name}/{root_path}', public=True, name='reserved')(lambda r: Response())
        self.assertEqual(app.url_path_for('reserved', name='one', root_path='two'), '/reserved/one/two')
        self.assertEqual(app.url_path_for('file', tail='a/café'), '/files/a/caf%C3%A9')
        self.assertEqual(app.url_path_for('id', identity=UUID(int=1), number=2),
                         '/ids/00000000-0000-0000-0000-000000000001/2')
        for params in ({'tail': '../x'}, {'tail': '/x'}, {'tail': 'a//b'}, {'tail': 'x\\y'}, {'tail': ''}, {},
                       {'tail': 'x', 'extra': 'y'}):
            with self.subTest(params=params), self.assertRaises(ConfigurationError):
                app.url_path_for('file', **params)
        for params in ({'identity': 'invalid', 'number': 1}, {'identity': UUID(int=1), 'number': True}):
            with self.assertRaises(ConfigurationError):
                app.url_path_for('id', **params)
        with self.assertRaises(ConfigurationError):
            app.url_path_for('missing')
        with self.assertRaises(ConfigurationError):
            app.route('/other', public=True, name='file')(lambda r: Response())
        for prefix in ('/bad/', 'relative', '/{dynamic}', '//host'):
            with self.assertRaises(ConfigurationError):
                Router(prefix=prefix)
