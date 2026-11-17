"""Explicit JSON input/output contracts independent of resource identity."""

import inspect
from dataclasses import dataclass, is_dataclass

from .errors import ConfigurationError, Issue, ValidationError
from .models import _Contract
from .openapi import _object
from .responses import JSONResponse, Response
from .route_docs import RouteDocs, _json_data


@dataclass(frozen=True, slots=True, init=False)
class DataclassJSON:
    model: type
    _contract: object

    def __init__(self, model):
        if not isinstance(model, type) or not is_dataclass(model):
            raise ConfigurationError('Select a dataclass JSON model.')
        object.__setattr__(self, 'model', model)
        object.__setattr__(self, '_contract', _Contract(model, frozenset()))

    def load(self, value):
        return self._contract.construct(value)

    def dump(self, value):
        return self._contract.encode(self.model, value)

    def json_schema(self, mode):
        if mode not in ('input', 'output'):
            raise ConfigurationError('Select input or output schema mode.')
        return _object(self._contract, 'create' if mode == 'input' else 'response')

    def snapshot(self):
        return self


@dataclass(frozen=True, slots=True, init=False)
class JSONAdapter:
    """Caller-owned validator/serializer with explicit detached JSON schemas."""

    load: object
    dump: object
    _input_schema: dict
    _output_schema: dict

    def __init__(self, *, load, dump, input_schema, output_schema):
        if not callable(load) or not callable(dump):
            raise ConfigurationError('JSON adapters require validator and serializer callables.')
        for name, schema in (('_input_schema', input_schema), ('_output_schema', output_schema)):
            if not isinstance(schema, dict):
                raise ConfigurationError('JSON adapter schemas must be JSON objects.')
            object.__setattr__(self, name, _json_data(schema))
        object.__setattr__(self, 'load', load)
        object.__setattr__(self, 'dump', dump)

    def json_schema(self, mode):
        if mode not in ('input', 'output'):
            raise ConfigurationError('Select input or output schema mode.')
        return _json_data(self._input_schema if mode == 'input' else self._output_schema)

    def snapshot(self):
        return JSONAdapter(load=self.load, dump=self.dump,
                           input_schema=self._input_schema, output_schema=self._output_schema)


def _select(schema):
    if schema is None:
        return None
    if isinstance(schema, type) and is_dataclass(schema):
        return DataclassJSON(schema)
    if type(schema) not in (DataclassJSON, JSONAdapter):
        raise ConfigurationError('Select a dataclass type, DataclassJSON or explicit JSONAdapter.')
    return schema.snapshot()


@dataclass(frozen=True, slots=True, init=False)
class JSONContract:
    input: DataclassJSON | JSONAdapter | None
    output: DataclassJSON | JSONAdapter | None
    status: int

    def __init__(self, *, input=None, output=None, status=200):
        if (input is None and output is None or type(status) is not int
                or not 200 <= status <= 299 or status in (204, 205)
                or output is None and status != 200):
            raise ConfigurationError('Select JSON input/output and a body-bearing success status.')
        object.__setattr__(self, 'input', _select(input))
        object.__setattr__(self, 'output', _select(output))
        object.__setattr__(self, 'status', status)

    def snapshot(self):
        return JSONContract(input=self.input, output=self.output, status=self.status)

    def documentation(self, docs):
        result = docs.snapshot() if docs is not None else RouteDocs()
        if self.input is not None:
            content = {'application/json': {'schema': self.input.json_schema('input')}}
            current = result._data.get('requestBody')
            if current is not None and (current.get('content') != content or current.get('required', True) is not True):
                raise ConfigurationError('Request documentation conflicts with the selected JSON contract.')
            result._data['requestBody'] = {**(current or {}), 'required': True, 'content': content}
        if self.output is not None:
            content = {'application/json': {'schema': self.output.json_schema('output')}}
            if not result._explicit_responses:
                result._data['responses'] = {}
            response = result._data['responses'].setdefault(str(self.status), {'description': 'Validated JSON response'})
            if 'content' in response and response['content'] != content:
                raise ConfigurationError('Response documentation conflicts with the selected JSON contract.')
            response['content'] = content
        return result

    async def read(self, request):
        data = await request.json()
        try:
            value = self.input.load(data)
            return await value if inspect.isawaitable(value) else value
        except (ConfigurationError, ValidationError):
            raise
        except ValueError:
            raise ValidationError(Issue((), 'validator')) from None

    async def response(self, value):
        if isinstance(value, Response):
            raise ConfigurationError('Validated output must return a model value.')
        try:
            data = self.output.dump(value)
            if inspect.isawaitable(data):
                data = await data
            return JSONResponse(data, status=self.status)
        except (ValidationError, ValueError):
            raise ConfigurationError('Handler output does not match its JSON contract.') from None
