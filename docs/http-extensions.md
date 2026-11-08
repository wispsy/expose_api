# HTTP extensions

## Streaming ownership

`StreamingResponse(source, chunk_size=65536, content_length=None)` owns one
nonblocking synchronous or asynchronous iterator of bytes. Each response is
single-use. Emitted chunks are at most chunk_size (maximum 1 MiB). A producer
owns the size of the bytes it allocates before yielding. Synchronous producers
must not block. Unknown-length bodies omit Content-Length; the development
runner retains its connection-close HTTP profile.

HEAD sends headers without iterating and closes the source. Ordinary completion,
disconnect, cancellation, producer errors and send failures close the iterator.
The disconnect listener begins after the handler returns; it does not compete
with handler-owned body parsing. Once headers start, errors propagate without
starting another response. Declared lengths must match actual bytes. Streaming
does not impose a total duration; the caller/server owns its deadline.

Response.prepare(scope) runs before response-start and may return a selected
Response. Application errors during preparation use the ordinary error boundary.
The final callable owns sending and cleanup after preparation.

These contracts follow the [ASGI HTTP event interface](https://asgi.readthedocs.io/en/latest/specs/www.html).

## Files and conditional requests

`FileResponse(path, filename=None, media_type=None, chunk_size=65536)` opens one
explicitly selected regular file during preparation. Missing, unreadable and
nonregular files return a public 404 without the path. FileResponse is single-use;
it transfers descriptor ownership to its stream, including HEAD, and reconciles
pending thread I/O before closing on cancellation. It does not root a directory
or prohibit caller-selected symlinks; StaticFiles owns those constraints.

MIME type is inferred with an octet-stream fallback. Download names use safe
ASCII fallback and UTF-8 filename* parameters. Metadata includes Last-Modified,
Accept-Ranges and a weak mtime/size ETag, which is not a content hash. GET/HEAD
honor If-None-Match before If-Modified-Since; 304 omits body and Content-Length.
GET supports one closed, open-ended or suffix byte range. Unsatisfiable,
malformed or multiple byte ranges return 416 with bytes */size. Other units are
ignored. HEAD ignores Range. If-Range accepts a current HTTP date; weak validators
cannot select a range, so tag-based If-Range falls back to the full file.

Metadata and bytes come from the same open file. Applications should replace
assets atomically; modifying an open file in place can change its contents or
trigger a post-start length error. The response is not a filesystem snapshot.
The selected conditional/range behavior follows [HTTP semantics](https://www.rfc-editor.org/rfc/rfc9110.html).

## Custom-route OpenAPI

Pass `docs=RouteDocs(summary='Read item', operation_id='read_item', ...)` to an
App/Router route. Defaults describe explicit methods and a generic 200 response.
Select request_body, responses, query/header/cookie parameters, tags, description
and security as OpenAPI JSON metadata. Path parameters come from converters;
int is nonnegative with the runtime's 20-digit bound, UUID has uuid format and
path includes x-path-converter. `include=False` hides a registration, including
explicit HEAD/OPTIONS. Generated HEAD has no response content. Arbitrary HTTP
methods remain usable and appear in x-http-methods rather than invalid OAS keys.

Metadata is copied at construction, registration and document generation.
Schemas are descriptive; handlers still own validation and actual status/shape.
One documented converter contract per normalized path and globally unique
operation IDs are required. A multi-method explicit ID receives method suffixes.
Hidden converter variants remain a runtime configuration choice.

Selected BearerAuth route authentication describes its scheme, 401 challenge,
403 and protected no-store policy. Other caller-owned schemes can be declared
with `app.openapi_security_scheme(name, definition)` and selected through
RouteDocs.security. Declarations never install authentication. Undefined scheme
references, duplicate IDs and incompatible metadata fail instead of emitting
ambiguous documents. Resource schemas and opt-in tabular paths are retained.
No documentation URL is automatically published.

The projection retains [OpenAPI 3.1.0](https://spec.openapis.org/oas/v3.1.0.html).
