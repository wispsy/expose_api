import unittest
from dataclasses import dataclass, field

from expose_api import App, Authenticated, ConfigurationError, DataclassJSON, JSONAdapter, JSONContract, JSONResponse, Principal, Response, RouteDocs, Router, TestClient


@dataclass
class Address:
    city: str


@dataclass
class Payload:
    address: Address
    count: int = 1
    labels: list[str] = field(default_factory=list)
    optional: str | None = None


@dataclass
class Receipt:
    count: int
    hidden: str = field(default='', metadata={'expose_api': {'write_only': True}})


class ContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_nested_defaults_nullable_models_and_runtime_docs_agree(self):
        app = App()
        @app.route('/typed', methods=('POST',), public=True, contract=JSONContract(input=Payload, output=Receipt, status=201))
        def handler(context):
            self.assertIsInstance(context.input, Payload)
            self.assertIsInstance(context.input.address, Address)
            self.assertEqual((context.input.labels, context.input.optional), ([], None))
            return Receipt(context.input.count, 'private')
        async with TestClient(app) as client:
            response = await client.post('/typed', json={'address': {'city': 'Paris'}})
            self.assertEqual((response.status, response.json), (201, {'count': 1}))
        operation = app.openapi()['paths']['/typed']['post']
        self.assertTrue(operation['x-runtime-validation'])
        incoming = operation['requestBody']['content']['application/json']['schema']
        self.assertEqual(incoming['required'], ['address'])
        self.assertFalse(incoming['additionalProperties'])
        outgoing = operation['responses']['201']['content']['application/json']['schema']
        self.assertEqual(set(outgoing['properties']), {'count'})
        self.assertNotIn('200', operation['responses'])
        self.assertIn('422', operation['responses'])

    async def test_input_errors_are_stable_value_free_and_do_not_call_handler(self):
        app, calls = App(), []
        app.route('/x', methods=('POST',), public=True, contract=JSONContract(input=Payload))(
            lambda r: calls.append(True))
        async with TestClient(app) as client:
            for data in ({'address': {'city': 42}}, {'address': {'city': 'x'}, 'secret unknown': 'secret value'},
                         {'address': {'city': 'x'}, 'count': True}, {}):
                result = await client.post('/x', json=data)
                self.assertEqual(result.status, 422)
                self.assertNotIn('secret', result.text)
            self.assertEqual((await client.post('/x', content=b'{broken', headers={'content-type': 'application/json'})).status, 400)
            self.assertEqual((await client.post('/x', content=b'{}')).status, 415)
        self.assertEqual(calls, [])

    async def test_authentication_authorization_and_hooks_precede_validation(self):
        principal, calls = [None], []
        app = App(authenticate=lambda r: principal[0], auth_challenge='Session')
        app.route('/x', methods=('POST',), access=Authenticated(scopes={'write'}),
                  dependencies=(lambda r: calls.append('hook'),), contract=JSONContract(input=Payload))(
            lambda r: Response())
        async with TestClient(app) as client:
            self.assertEqual((await client.post('/x', json={})).reads, 0)
            principal[0] = Principal('a')
            self.assertEqual((await client.post('/x', json={})).reads, 0)
            self.assertEqual(calls, [])
            principal[0] = Principal('a', scopes=frozenset({'write'}))
            result = await client.post('/x', json={})
            self.assertEqual(result.status, 422)
            self.assertEqual(result.headers['cache-control'], 'no-store')
            self.assertEqual(calls, ['hook'])

    async def test_invalid_outputs_are_500_before_start_and_never_422(self):
        for value in ({'count': 1}, Receipt('secret output'), Response(b'bypass')):
            app = App()
            app.route('/x', public=True, contract=JSONContract(output=Receipt))(lambda r: value)
            async with TestClient(app) as client:
                with self.assertLogs('expose_api', 'ERROR') as logs:
                    result = await client.get('/x')
            self.assertEqual(result.status, 500)
            self.assertNotIn('secret', result.text + str(logs.output))
            self.assertEqual(sum(event['type'] == 'http.response.start' for event in result.messages), 1)

    async def test_router_snapshot_head_and_identity_free_schema(self):
        router = Router()
        router.route('/x', public=True, contract=JSONContract(output=Receipt))(lambda r: Receipt(2))
        app = App()
        app.include_router(router, prefix='/api')
        async with TestClient(app) as client:
            get, head = await client.get('/api/x'), await client.head('/api/x')
            self.assertEqual(get.json, {'count': 2})
            self.assertEqual(head.body, b'')
            self.assertEqual(head.headers['content-length'], get.headers['content-length'])
        head_docs = app.openapi()['paths']['/api/x']['head']['responses']['200']
        self.assertNotIn('content', head_docs)
        self.assertEqual(DataclassJSON(Receipt).dump(Receipt(3)), {'count': 3})

    async def test_async_caller_owned_adapter_and_sanitized_validator_errors(self):
        schema = {'type': 'integer', 'minimum': 1}
        async def load(value):
            if type(value) is not int or value < 1:
                raise ValueError('submitted secret')
            return value
        async def dump(value):
            if type(value) is not int:
                raise ValueError('output secret')
            return value
        adapter = JSONAdapter(load=load, dump=dump, input_schema=schema, output_schema=schema)
        app = App()
        app.route('/x', methods=('POST',), public=True, contract=JSONContract(input=adapter, output=adapter))(
            lambda r: r.input + 1)
        schema['type'] = 'string'
        async with TestClient(app) as client:
            self.assertEqual((await client.post('/x', json=1)).json, 2)
            response = await client.post('/x', json=0)
            self.assertEqual(response.status, 422)
            self.assertNotIn('secret', response.text)
        self.assertEqual(app.openapi()['paths']['/x']['post']['requestBody']['content']['application/json']['schema']['type'], 'integer')

    def test_conflicting_docs_and_invalid_contracts_fail_at_registration(self):
        for kwargs in ({}, {'input': dict}, {'output': Receipt, 'status': 204}, {'input': Payload, 'status': 201}):
            with self.assertRaises(ConfigurationError):
                JSONContract(**kwargs)
        for docs in (RouteDocs(request_body={'content': {'application/xml': {'schema': {}}}}),
                     RouteDocs(responses={'200': {'description': 'Wrong', 'content': {'application/json': {'schema': {'type': 'string'}}}}})):
            with self.assertRaises(ConfigurationError):
                Router().route('/x', public=True, contract=JSONContract(input=Payload, output=Receipt), docs=docs)(lambda r: Receipt(1))
