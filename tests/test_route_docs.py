import unittest
from dataclasses import dataclass

from expose_api import App, BearerAuth, ConfigurationError, JSONResponse, MemoryStore, Principal, Response, RouteDocs, Router
from asgi_client import request


class RouteDocumentationTests(unittest.IsolatedAsyncioTestCase):
    async def test_paths_converters_head_options_and_runtime_agree(self):
        app = App()
        app.route('/items/{number:int}/{identity:uuid}', public=True,
                  docs=RouteDocs(operation_id='read_item', summary='Read one', tags=('items',),
                                 parameters=({'name': 'q', 'in': 'query', 'schema': {'type': 'string'}},)))(
            lambda r: JSONResponse({'number': r.path_params['number']}))
        spec = app.openapi()
        path = spec['paths']['/items/{number}/{identity}']
        self.assertEqual(set(path), {'get', 'head', 'options'})
        operation = path['get']
        self.assertEqual(operation['operationId'], 'read_item')
        self.assertEqual(operation['parameters'][0]['schema']['type'], 'integer')
        self.assertEqual(operation['parameters'][1]['schema']['format'], 'uuid')
        self.assertEqual(path['options']['parameters'][:2], operation['parameters'][:2])
        self.assertNotIn('content', path['head']['responses']['200'])
        result = await request(app, '/items/2/00000000-0000-0000-0000-000000000001')
        self.assertEqual(result.json, {'number': 2})
        options = await request(app, '/items/2/00000000-0000-0000-0000-000000000001', 'OPTIONS')
        self.assertEqual(options.headers[b'allow'].decode(), path['options']['responses']['204']['headers']['Allow']['schema']['const'])

    async def test_explicit_schemas_are_detached_and_do_not_install_validation(self):
        request_body = {'required': True, 'content': {'application/json': {'schema': {
            'type': 'object', 'properties': {'value': {'type': 'integer'}}}}}}
        docs = RouteDocs(operation_id='create_item', request_body=request_body,
                         responses={'201': {'description': 'Created', 'content': {'application/json': {'schema': {'type': 'object'}}}}})
        router = Router(prefix='/api')
        @router.route('/items', methods=('POST',), public=True, docs=docs)
        async def create(context):
            return JSONResponse(await context.json(), status=201)
        app = App()
        app.include_router(router)
        request_body['content']['application/json']['schema']['properties'].clear()
        spec = app.openapi()
        operation = spec['paths']['/api/items']['post']
        self.assertIn('value', operation['requestBody']['content']['application/json']['schema']['properties'])
        self.assertFalse(operation['x-runtime-validation'])
        operation['responses'].clear()
        self.assertIn('201', app.openapi()['paths']['/api/items']['post']['responses'])
        result = await request(app, '/api/items', 'POST', data={'value': 'caller validates this'})
        self.assertEqual((result.status, result.json['value']), (201, 'caller validates this'))

    async def test_authentication_metadata_challenges_and_cache_policy(self):
        app = App(authenticate=BearerAuth(lambda token: Principal('user')), auth_challenge='Bearer')
        app.route('/private', authenticate=True, authorize=lambda r: True,
                  docs=RouteDocs(summary='Protected'))(lambda r: Response())
        spec = app.openapi()
        operation = spec['paths']['/private']['get']
        self.assertEqual(operation['security'], [{'BearerAuth': []}])
        self.assertEqual(spec['components']['securitySchemes']['BearerAuth'], {'type': 'http', 'scheme': 'bearer'})
        denied = await request(app, '/private')
        self.assertEqual(denied.status, 401)
        self.assertIn('WWW-Authenticate', operation['responses']['401']['headers'])
        self.assertEqual(operation['responses']['401']['headers']['Cache-Control']['schema']['const'], 'no-store')

    def test_hidden_explicit_methods_nonstandard_methods_and_ids(self):
        app = App()
        app.route('/visible', public=True)(lambda r: Response())
        app.route('/visible', methods=('HEAD',), public=True, docs=RouteDocs(include=False))(lambda r: Response())
        app.route('/hidden', public=True, docs=RouteDocs(include=False))(lambda r: Response())
        app.route('/cache', methods=('PURGE',), public=True)(lambda r: Response())
        app.route('/many', methods=('GET', 'POST'), public=True, docs=RouteDocs(operation_id='many'))(lambda r: Response())
        spec = app.openapi()
        self.assertNotIn('/hidden', spec['paths'])
        self.assertNotIn('head', spec['paths']['/visible'])
        self.assertEqual(spec['paths']['/cache']['x-http-methods'], ['PURGE'])
        self.assertEqual(spec['paths']['/many']['post']['operationId'], 'many_post')

    def test_custom_security_and_registration_freeze(self):
        app = App()
        scheme = {'type': 'apiKey', 'in': 'header', 'name': 'X-Token'}
        app.openapi_security_scheme('SelectedToken', scheme)
        scheme['name'] = 'Changed'
        app.route('/selected', public=True, docs=RouteDocs(security=[{'SelectedToken': []}]))(lambda r: Response())
        self.assertEqual(app.openapi()['components']['securitySchemes']['SelectedToken']['name'], 'X-Token')
        with self.assertRaises(ConfigurationError):
            app.openapi_security_scheme('SelectedToken', {'type': 'http', 'scheme': 'basic'})
        missing = App()
        missing.route('/x', public=True, docs=RouteDocs(security=[{'Missing': []}]))(lambda r: Response())
        with self.assertRaises(ConfigurationError):
            missing.openapi()

    def test_collisions_and_metadata_validation(self):
        @dataclass
        class Note:
            id: str
        app = App()
        app.include(Note, store=MemoryStore(), public=True)
        app.route('/health', public=True, docs=RouteDocs(operation_id='resource_0_list'))(lambda r: Response())
        with self.assertRaises(ConfigurationError):
            app.openapi()
        for paths in (('/x/{a:int}', '/x/{a:str}'), ('/x/{a}', '/x/{b:int}')):
            app = App()
            for path in paths:
                app.route(path, public=True)(lambda r: Response())
            with self.assertRaises(ConfigurationError):
                app.openapi()
        for options in ({'responses': {'204': {'description': 'Empty', 'content': {}}}}, {'summary': 1},
                        {'parameters': [{'name': 'id', 'in': 'path'}]}, {'responses': {'200': {'description': 'OK', 'headers': []}}},
                        {'security': {'invalid': []}}, {'responses': {'bad': {'description': 'No'}}}):
            with self.subTest(options=options), self.assertRaises(ConfigurationError):
                RouteDocs(**options)
