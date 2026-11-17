# Explicit web application contracts

The EXP-WEB work extends the completed HTTP foundation before the Pretty Notebook
compatibility harness. Core imports and all features here use the standard
library. Application credentials, databases and rendering engines remain
caller-owned.

## Typed request providers

Register providers before serving with `app.provide(Service, provider)`.
Providers receive the request and may return a service, an awaitable service,
or a synchronous/asynchronous context manager yielding the service. Resolve
explicitly with `service = await request.resolve(Service)`. The result must be
an instance of the registered type. No handler signature inspection is involved.

Each request caches both successful and failed provider executions. Concurrent
callers share one execution; dynamic and concurrent dependency cycles fail with
ConfigurationError. Providers may resolve other registered services. Synchronous
providers and context-manager methods run inline and must remain nonblocking.

Resources stay open through exception handlers and streamed response completion.
Cleanup cancels and settles unfinished providers, then exits acquired contexts
in reverse order. Contexts see the application/transport error when present;
they cannot suppress it. Cleanup is reconciled through repeated cancellation.
Each managed provider remains in its own task through context exit, preserving
task identity and context-variable tokens. Its context variables remain private
to that provider; handlers use the returned service explicitly.
An error during cleanup after response-start propagates to the server; no second
response is attempted. A retained request cannot resolve services after closing.

`with app.override_providers({Service: replacement}): ...` selects task-local
overrides for registered types. Nested scopes restore the previous selection,
including failures; concurrent contexts do not mutate the application registry.
Request scopes snapshot overrides on first resolution. Overrides do not bypass
route authentication or authorization.

## Authenticated access policies

`@app.route('/admin', access=Authenticated(scopes={'admin'}, tenant=True,
checks=(can_read,)))` selects identity verification and authorization together.
The application must configure its caller-owned authenticator and challenge.
Do not combine `access` with `public`, `authorize` or `authenticate` flags.
Existing explicit authorization callbacks retain their original behavior.

Missing identity returns 401 with the configured challenge. All selected scopes
must be present; `tenant=True` requires tenant information and a string requires
an exact tenant match. Missing permissions return 403 before dependencies or body
reads. Ordered synchronous/asynchronous checks receive the verified request,
must return bool and short circuit on denial. Checks can resolve typed services.
Tenant ownership of individual records still belongs to record policies or
application permission checks; selecting a tenant does not infer record access.

Protected successes and errors force no-store. Router inclusion preserves policy
selection. OpenAPI describes required scopes/tenant and the existing challenge;
metadata cannot select an anonymous alternative for authenticated routes.
Credential verification, session/account storage and token issuance remain
application-owned.

## Public async testing client

```python
from expose_api import TestClient

async with TestClient(app) as client:
    result = await client.get('/healthz')
    assert result.status == 200
```

The client drives one explicit ASGI startup/shutdown lifecycle in the current
event loop. It also accepts arbitrary ASGI applications implementing lifespan.
Startup/shutdown refusal, raw application exceptions, invalid framing and peer
timeouts remain visible to the test. Exiting closes WebSocket sessions and
cancels unfinished requests before shutdown, then restores overrides.

HTTP helpers support get/post/put/patch/delete/head/options and arbitrary uppercase
methods through `request(method, path)`. Select `json=`, `data=` (urlencoded),
`content=` or raw ASGI `events=`; `params=` preserves repeated query pairs.
Pass `disconnect=True` to queue a disconnect after input events. Ordinary requests
keep receive open after the body so streams can complete. Results expose status,
body/text/json properties, raw_headers and repeated `headers.getlist(name)`,
messages, receive-call count and whether the response completed. Disconnected
requests may have status 0 and an incomplete response.

The client buffers at most max_response_size (default 16 MiB), enforces event
ordering and uses a five-second default timeout. It makes no network requests,
follows no redirects and accepts only local request paths on its selected
base_url. The standard-library cookie jar preserves path/domain/secure/expiry
rules and repeated Set-Cookie; this does not emulate browser SameSite or CORS.
`root_path`, default headers and explicit per-request scope overrides support
mount/protocol tests. Raw scope/events overrides are intentionally test inputs.

`TestClient(app, providers={Type: replacement}, authenticate=verifier)` uses
task-local application overrides. Authentication overrides also apply to
resource subscriptions; permission policies still run. Applications without
these override interfaces can use the remaining client features unchanged.

`async with client.websocket(path, headers=..., subprotocols=...) as socket`
requires acceptance, exposes its accept event, and supports send_text,
send_bytes, send_json, receive_event and receive_json. Message size and a
single buffered outgoing message bound producer output. Rejection and premature
closure raise ProtocolError; raw receive_event exposes later close codes.
Timeouts cancel application tasks; application cleanup must cooperate with
cancellation, and owned cleanup is allowed to settle before exit.

The event shapes and lifecycle follow the
[ASGI HTTP/WebSocket](https://asgi.readthedocs.io/en/latest/specs/www.html) and
[lifespan](https://asgi.readthedocs.io/en/latest/specs/lifespan.html) specifications.

## Validated JSON handlers

```python
from dataclasses import dataclass
from expose_api import JSONContract

@dataclass
class Greeting:
    name: str

@app.route('/greet', methods=('POST',), public=True,
           contract=JSONContract(input=Greeting, output=Greeting))
def greet(request):
    return Greeting(name='Hello, ' + request.input.name)
```

JSONContract selects runtime input and/or output validation. Dataclass models
reuse resource field validation, defaults, nested values, visibility and strict
JSON types, without requiring an identity field. Authorization and ordered
request hooks run before the bounded JSON reader and validation. The parsed
model is request.input. Missing fields and invalid values use the existing
value-free 422 envelope; malformed JSON/media retain 400/415. Constructors and
custom post-init validators follow the existing dataclass model contract.

With output selected, return the typed value; the contract validates, serializes
and emits JSONResponse at its selected success status (default 200). Returning
a Response cannot bypass output validation. Invalid output is a sanitized 500
before headers. Input-only routes continue returning explicit Response objects;
file/stream/HTML routes can use that profile. Path converters and repeated query
fields keep their existing explicit interfaces; no signature injection or query
coercion is inferred.

The same selected model contracts generate request/response OpenAPI schemas and
validation errors. Conflicting RouteDocs body or success-content declarations
fail registration. Additional descriptive error metadata remains available.
HEAD suppresses bytes while retaining validated representation headers.

`DataclassJSON(Model)` exposes load/dump/json_schema independently.
`JSONAdapter(load=..., dump=..., input_schema=..., output_schema=...)` selects
caller-owned synchronous/asynchronous validators and serializers with detached
schema metadata. ValueError from input validators becomes a value-free 422;
framework ConfigurationError and unexpected failures stay 500. A validator may
raise the framework ValidationError for deliberate public paths/codes. Keep
submitted values out of those details. Output failures remain 500. Adapter
callbacks own conformance to their supplied JSON schemas.

For an application already using Pydantic, pass model_validate as load, a
callback calling model_dump(mode='json') as dump, and the corresponding
model_json_schema documents. Optional Enforce Type or other validators can use
the same callback boundary. The framework does not import/install those engines.

## Development arenas

The canonical queue is [Project 7](https://github.com/orgs/wispsy/projects/7):

| Arena | Contract |
| --- | --- |
| [EXP-WEB-01](https://github.com/wispsy/expose_api/issues/132) | Typed request dependencies and resource cleanup |
| [EXP-WEB-02](https://github.com/wispsy/expose_api/issues/133) | Authenticated access policy composition |
| [EXP-WEB-03](https://github.com/wispsy/expose_api/issues/134) | Public asynchronous ASGI testing client |
| [EXP-WEB-04](https://github.com/wispsy/expose_api/issues/135) | Validated custom handler contracts |
| [EXP-WEB-05](https://github.com/wispsy/expose_api/issues/136) | Opt-in security middleware and 0.2.0 preparation |

These changes do not switch Pretty Notebook factories or publish a package.
Each arena receives local checks and one remote integration PR. Earlier CI is
collected while later work proceeds; the final main CI is deliberately left
running for subsequent review.
