# General HTTP applications

This checkout adds explicit HTTP primitives alongside dataclass resources.
The published 0.1.0 release retains its original API. The staged enhancement
and application migration gates are in [the plan](../IMPLEMENTATION_PLAN.md).

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
media type before reading. Multipart and unbounded streaming are follow-up
work. Concurrent readers fail, and a failed, disconnected or canceled reader
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

`app.openapi()` currently describes resources; custom-route metadata is tracked
separately by WEB-09. Handler registration does not infer validation schemas.

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
