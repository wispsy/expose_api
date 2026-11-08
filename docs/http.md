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
