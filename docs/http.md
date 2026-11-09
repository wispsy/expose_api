# General HTTP applications

This checkout adds explicit HTTP primitives alongside dataclass resources.
The published 0.1.0 release retains its original API. The staged enhancement
and application migration gates are in [the plan](../IMPLEMENTATION_PLAN.md).
See [HTTP extensions](http-extensions.md) for streaming ownership and the
following bounded file/upload/composition profiles.

`Response(content=b'', status=200, headers=(), media_type=None)` accepts bytes
or UTF-8 text. Headers may be a dictionary or ordered pairs; repeated pairs
are preserved. Response owns Content-Length and rejects conflicting framing
and header injection. HEAD retains headers and suppresses the body; 204, 205
and 304 reject nonempty content. Transport errors propagate to the server.

`JSONResponse(value)` emits compact UTF-8 JSON and rejects non-finite numbers.
`HTMLResponse(text)` marks trusted HTML; escaping remains the renderer's job.
`RedirectResponse(url, status=307)` sets an encoded Location and accepts 301,
302, 303, 307 or 308. Applications must validate untrusted redirect destinations.

Responses support `set_cookie(name, value, secure=True, httponly=True)` and
`delete_cookie(name)` with matching path/domain options. Repeated Set-Cookie
headers remain distinct. SameSite defaults to lax; SameSite none and secure
cookie prefixes require secure attributes. Expiry accepts an aware datetime.
`HTTPException(status, detail, headers=())` carries intentionally public error
text; do not include credentials in detail.

`HTTPRequest` is the separate custom-handler request; resource authorization
continues to receive `Request`. It exposes method, path, copied scope/state,
path_params, principal and application. `header(name)` returns a value only
when exactly one exists. `headers.getlist(name)` and
`query_params.getlist(name)` preserve repetitions; mapping reads select the
last value. Query strings allow 8 KiB and 128 fields, with strict percent and
UTF-8 decoding. Cookies allow one Cookie header of at most 8 KiB.

`await request.body()` consumes bounded ASGI events once and caches the bytes.
It checks actual size and Content-Length. `json()` shares the resource JSON
rules; `form()` supports UTF-8 urlencoded forms with 128 fields. Both validate
media type before reading, including complete token/quoted-string parameters,
unique parameter names and a UTF-8 charset. Unknown well-formed parameters do
not select a different parser. Malformed parameters return 415 before consuming
the body. Multipart uses the separately selected bounded
profile below; unbounded request streaming remains separate. Concurrent readers
fail, and a failed, disconnected or canceled reader
cannot restart consumption. A route uses its application's max_body_size.

Register custom handlers separately from resources:

```python
from expose_api import App, JSONResponse

app = App()

@app.route('/healthz', public=True)
def health(request):
    return JSONResponse({'ready': True})

@app.route('/items/{number:int}', methods=('GET',), public=True)
async def item(request):
    return JSONResponse({'number': request.path_params['number']})
```

Handlers accept one HTTPRequest and return an explicit Response. Awaitable
results are awaited; synchronous handlers run inline and must be nonblocking.
Paths support whole-segment `{name}`, `{name:str}`, `{name:int}`, `{name:uuid}`
and final `{name:path}`. Integers match 1–20 nonnegative decimal digits.
Routes retain registration order, do not redirect trailing slashes and freeze
on first ASGI use. Duplicate method/patterns and resource namespace collisions
fail at registration. Custom HTTP routes do not register WebSockets.

HEAD uses explicit HEAD or GET, suppressing bytes. Generated OPTIONS and 405
report the union of matching methods. Routes require public=True or an explicit
boolean authorize callback evaluated before the handler/body reader. Protected
results and errors use no-store. HTTPException uses the existing error envelope
with code http_error. Unexpected failures remain redacted.

`app.openapi()` describes resources and custom HTTP routes. RouteDocs selects
explicit metadata, schemas and visibility; registration does not infer or install
validation schemas. See [custom-route documentation](http-extensions.md).

Use `Router(prefix='/api')` with the same route decorator, then
`app.include_router(router, prefix='/v1', name_prefix='v1:')`. Routers can include
other routers. Each inclusion copies the current registrations in order and
fails atomically on collisions; later router changes do not change applications.
Names are optional, bounded and unique when provided. Use name_prefix when
including named routes multiple times.

`app.url_path_for('item', number=1)` returns a local path.
`request.url_for('item', number=1)` includes the ASGI root_path. Both require
exact parameter names and matching converter values; UUID requires a UUID
object, int requires an ordinary nonnegative integer. Strings are encoded once;
path parameters allow safe multiple segments. Reversal rejects dot segments,
empty segments, slashes in ordinary strings and backslashes. Host headers never
contribute an authority. Neither method creates an absolute external URL.

`app.add_middleware(factory, **options)` uses ordinary ASGI middleware:
factory(inner_app, **options) returns an ASGI callable. The first registered
factory is outermost. Factories are built once on first use and registration
then freezes. HTTP errors before response-start are sanitized; errors after
start, transport failures, cancellation and protocol errors propagate. The
outer boundary suppresses HEAD bodies even for middleware or mounted apps.

`app.mount('/static', selected_asgi_app)` delegates that HTTP namespace on whole
segment boundaries. Mounts cannot overlap other mounts, custom handlers or
resources. The child receives a copied scope/state, extended root_path and
preserved full path/raw_path/query (relative parent paths are normalized with
the root prefix). The child owns its authentication. Parent lifespan runs once;
mounted child lifespans require explicit ownership in that parent lifespan.
StaticFiles supplies an explicit rooted asset application; arbitrary mounts
do not imply file serving or child WebSocket dispatch. Named mounts support
`request.url_for('assets', path='css/site.css')` with safe relative paths.

Routes can select `authenticate=True` with an explicit authorize callback.
The App must have authenticate and auth_challenge configured. The authenticator
receives HTTPRequest and returns a verified Principal or None; BearerAuth works
with both request interfaces. Missing identity returns 401 with the challenge;
authorization false returns 403. Authentication runs before authorization, then
dependencies, then the handler. All protected responses and errors force one
Cache-Control: no-store value, including custom error responses.

`dependencies=(hook, ...)` runs explicit request hooks in order, once per callable
identity per request. Hooks receive HTTPRequest, may be async, and return None.
They can populate request.state or raise HTTPException to stop the request.
There is no signature injection, yielded dependency context or return-value
injection. Caller-owned resource cleanup belongs in lifespan or handler context
managers. Authentication, authorization and hooks do not implicitly read a body.

`@app.exception_handler(ValueError)` registers a `(request, exception)` handler
returning Response. The closest type in the exception's method resolution order
wins. Explicit registrations may override default resource errors too; without
one, existing resource error envelopes are retained. Route handlers reuse their
request, principal and cached body/state. An error outside a custom route receives
an application-bound request whose unowned body cannot be consumed. Handler
failures are sanitized without recursion. Disconnect, cancellation, protocol and
post-response-start/transport failures bypass these handlers. Registration
freezes on first ASGI use. Error text is public only when explicitly selected.
