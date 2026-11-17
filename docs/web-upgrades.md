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
an instance of the registered type; the resolve annotation retains that type
for static callers. No handler signature inspection is involved.

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

For an application already using [Pydantic](https://docs.pydantic.dev/latest/concepts/serialization/), pass model_validate as load, a
callback calling model_dump(mode='json') as dump, and the corresponding
model_json_schema documents. Optional Enforce Type or other validators can use
the same callback boundary. The framework does not import/install those engines.

## Opt-in security middleware

Register middleware explicitly with `app.add_middleware(Profile, **options)`.
The first registered middleware is outermost. A useful order is ProxyHeaders,
SecurityHeaders, TrustedHost, CORS, CSRF, then a rate-limit decision. Security
profiles do not install credential verification or replace route permissions.

`TrustedHostMiddleware(allowed_hosts=('example.test', '*.assets.test', '[::1]'))`
requires one well-formed Host. Matching is case insensitive, ignores a valid
numeric port and requires complete hostname/subdomain boundaries. A wildcard
matches subdomains, excluding its apex. Missing, repeated, malformed and
unlisted hosts return 400 before body reads; WebSockets close with 1008.
There is no automatic www redirect or universal wildcard. Configuration selects
hostnames/IPs without ports; IPv6 addresses use brackets.

`ProxyHeadersMiddleware(trusted_proxies=('127.0.0.1', '10.0.0.0/8'))` selects
the immediate peer networks that may supply X-Forwarded-For/Proto. Traverse the
For chain from right to left, discarding trusted proxies, and select the nearest
untrusted address; if every hop is trusted, select the leftmost address. The
effective client port is unknown (0). Require one field of at most 32 bare IP
addresses/8 KiB and one optional http/https Proto value. WebSocket schemes map
to ws/wss. Invalid trusted forwarding returns 400/1008; untrusted forwarding is
ignored. Strip all Forwarded, X-Forwarded-* and X-Real-IP headers before passing
on the copied scope. Host and root_path are never inferred from these headers.
The configured proxies must overwrite or correctly append forwarding fields;
the middleware cannot authenticate arbitrary upstream header authors.

`SecurityHeadersMiddleware(headers=..., hsts_max_age=None)` supplies nosniff,
DENY framing, strict-origin-when-cross-origin referrers and a same-origin CSP
with object-src none, base-uri self and frame-ancestors none. Applications may
override these selected values explicitly for their assets. No cookies or
framing headers can be configured here. HSTS is emitted on HTTPS only when an
age is selected; forwarding trust must precede it. Configured headers replace
inner values while preserving repeated Set-Cookie and unrelated headers.
Native App applies selected security/CORS transforms at its final response
boundary, including sanitized errors from inner middleware. Cancellation,
transport errors and post-start failures retain their existing ownership.

`CORSMiddleware(origins=('https://app.example.test',), methods=('GET', 'POST'),
headers=('content-type', 'x-csrf-token'), credentials=True)` selects finite
origins/methods/request headers and optional exposed headers/max_age. HTTP(S)
origins have no paths, credentials or null value. Default ports and host case
are normalized for matching. A wildcard origin is allowed only without
credentials; wildcard methods/headers are not selected. Preflight validates
the requested method/header set and returns 204 without body reads. Allowed
actual responses, including errors, receive the selected CORS fields and Vary;
preflights vary by origin, method and requested headers. Existing Vary values
are retained. Disallowed/ambiguous origins, methods or preflights return 403
with no-store. Requests without Origin pass through. CORS does not authenticate
requests, and nonbrowser clients can omit Origin. The profile follows the
[Fetch CORS protocol](https://fetch.spec.whatwg.org/#http-cors-protocol).

`CSRFMiddleware(origins=(...), token=expected_token, cookie_name='session',
header_name='x-csrf-token', always=False)` protects unsafe HTTP methods when the
selected session cookie is present. `always=True` also protects login or other
unsafe routes before a session cookie exists. GET/HEAD/OPTIONS pass through.
Require one allowed Origin, or an allowed Referer origin when Origin is absent,
plus one token header. Missing, duplicate or malformed cookie/origin/token
fields return a value-free 403 before body reads. The finite cookie profile
rejects duplicate names and ambiguous/partially parsed fields.

The caller-owned sync/async token callback receives request metadata and must
verify the session cookie, returning its expected session-bound token or None.
The expected token must be unpredictable, session-specific and 16–4096 printable
ASCII characters/bytes. Comparison uses constant-time byte comparison; the token
is never echoed in an error. This is an expected session token profile, not an
unsigned double-submit cookie comparison. The application owns token issuance,
rotation and session lifecycle. It is a header-token profile; form submissions
must supply the header through their client or use an application-owned form
policy. Selecting a token in a JSON/form body is not inferred. See
[OWASP CSRF prevention](https://cheatsheetseries.owasp.org/cheatsheets/Cross-Site_Request_Forgery_Prevention_Cheat_Sheet.html).

`RateLimitMiddleware(decide=callback)` asks a caller-owned sync/async decision
function for None (allow) or integer retry seconds in 0–86,400 (429 with
Retry-After/no-store). No implicit counter or backend is installed. Shared,
atomic storage and key selection are application responsibilities when limits
must span workers. CSRF token and rate-limit callbacks receive metadata-only
HTTPRequest objects; body consumption is disabled. Route-authenticated identity
has not yet been resolved at this middleware stage. A caller may instead use
an ordered route dependency for decisions requiring that verified principal.

Every profile passes lifespan through. TrustedHost/ProxyHeaders apply to
WebSockets; CORS, CSRF, security response headers and rate-limit decisions apply
to HTTP. Existing resource WebSocket origin/authentication checks still run.
No profile changes cookie flags, stores credentials, enables proxy trust or
publishes an application unless explicitly selected/configured by the caller.

## Version preparation and acceptance

Version 0.2.0 includes the completed HTTP foundation and these five framework
arenas, including provider task-ownership qualification. Package, import and
lockfile versions agree. Python 3.14 and the existing optional requirements are
unchanged. The distribution gate verifies module inventories, passive imports,
metadata, source rebuild parity and the complete base suite in clean wheel and
source installations. Installed receipts explicitly retain the new dependency,
policy, testing, contract and middleware module counts. Existing optional-profile
and Linux/macOS/Windows jobs remain selected in CI.

The composed acceptance case covers trusted proxy/host selection, HTTPS headers,
credentialed CORS, session-token CSRF, the rate-limit hook, authenticated tenant
and scope checks, request-managed services, typed JSON and the public client.
Pretty Notebook's actual factories, ORM, validators, publishing protocol and
deployment still require the PNB-WEB compatibility phase. Package publication
also remains a separate authorized operation after final CI review.

## Development arenas

The canonical queue is [Project 7](https://github.com/orgs/wispsy/projects/7):

| Arena | Contract |
| --- | --- |
| [EXP-WEB-01](https://github.com/wispsy/expose_api/issues/132) | Typed request dependencies and resource cleanup |
| [EXP-WEB-01A](https://github.com/wispsy/expose_api/issues/141) | Provider context/task ownership qualification |
| [EXP-WEB-02](https://github.com/wispsy/expose_api/issues/133) | Authenticated access policy composition |
| [EXP-WEB-03](https://github.com/wispsy/expose_api/issues/134) | Public asynchronous ASGI testing client |
| [EXP-WEB-04](https://github.com/wispsy/expose_api/issues/135) | Validated custom handler contracts |
| [EXP-WEB-05](https://github.com/wispsy/expose_api/issues/136) | Opt-in security middleware and 0.2.0 preparation |

These changes do not switch Pretty Notebook factories or publish a package.
Each arena receives local checks and one remote integration PR. Earlier CI is
collected while later work proceeds; the final main CI is deliberately left
running for subsequent review.
