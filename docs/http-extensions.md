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
Repeated cancellation also waits for an in-flight open/read/close to settle
before releasing its descriptor, then propagates cancellation. Upload spooling
uses the same ownership rule for thread-backed writes and cleanup.

MIME type is inferred with an octet-stream fallback. Download names use safe
ASCII fallback and UTF-8 filename* parameters. Metadata includes Last-Modified,
Accept-Ranges and a weak mtime/size ETag, which is not a content hash. GET/HEAD
honor If-None-Match before If-Modified-Since; 304 omits body and Content-Length.
Repeated If-None-Match fields form one ordered list, bounded to 8 KiB and 128
tags. Their presence suppresses date fallback even if malformed or over limits;
ambiguous repeated singleton dates are ignored.
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

## Bounded multipart uploads

Use `async with request.multipart(limits=MultipartLimits(...)) as form:`.
The context owns every UploadFile, including failures/cancellation during parsing
or handler execution. Ordered repeated names use form.getlist/multi_items; values
are UTF-8 strings or UploadFile objects with filename, content_type, size and
async read/seek/close. Filenames are untrusted metadata and never destinations.
Files must be consumed or copied before the context exits.

The selected native profile buffers a complete body bounded by both App and
MultipartLimits before parsing, then spools file parts above spool_threshold.
Defaults: 8 MiB total/part, 64 KiB per field, 8 KiB per header block, 64 parts,
64 fields, 16 files and 256 KiB spooling threshold. Configure App.max_body_size
as well; its default 1 MiB remains an upper bound. Byte limits have a 64 MiB
hard ceiling, headers 64 KiB and counts 1,024. Duplicate names count individually.

Require multipart/form-data with one 1–70 character ASCII boundary and CRLF
framing. Fields use UTF-8 text/plain. Quoted UTF-8 name/filename parameters are
supported. Reject preamble/epilogue, folded/duplicate/unknown part headers,
transfer encodings, nested multipart, filename*, unsupported charsets and
malformed quoting/boundaries. Limit failures use 413, malformed input 400 and
wrong media 415, without submitted values. Successful raw body reads cannot
bypass a later lower parser limit. This profile is deliberately finite rather
than a general MIME parser or incremental unbounded upload API.

See [multipart/form-data](https://www.rfc-editor.org/rfc/rfc7578.html) for the
underlying format; the supported subset above is the application contract.

## Rooted static assets

`app.mount('/static', StaticFiles(directory), name='assets')` selects an existing
asset tree. Named mounts share the route-name namespace and reverse through
app.url_path_for or request.url_for with one relative path argument. Mounted
root paths and Unicode encoding are retained; Host never supplies an authority.

StaticFiles serves regular files through FileResponse, including HEAD,
validators and single ranges. GET/HEAD/OPTIONS are supported; other methods
return 405/Allow. Directories, missing assets and rejected paths return 404 with
no listing, index inference or redirect. Defaults are revalidation caching and
nosniff. The caller selects the assets, MIME overrides and caching headers.

Paths are decoded once by the ASGI server. Reject dot/empty segments,
backslashes, controls, portable reserved names, Windows streams and trailing
dot/space names. Hidden segments require allow_hidden=True. Symlinks/reparse
points below the selected resolved root are rejected. On systems with directory
descriptors, open each component with no-follow flags. The portable fallback
checks reparse points, containment and opened identity; its tree/ancestors must
remain trusted and cannot be concurrently mutated by a hostile filesystem
actor. This is a trusted deployment asset profile, not an upload publication
service or filesystem sandbox. Use atomic file replacement and keep untrusted
writes outside the asset tree.

## Caller-owned templates

`TemplateRenderer(render).response(request, name, context, status=200, headers=())`
is asynchronous and returns HTMLResponse. A renderer receives a validated
relative loader name (excluding POSIX and Windows drive-qualified paths) and
a fresh top-level context dictionary containing request
and local url_for. Those keys cannot be overridden. A renderer may return text
or an awaitable of text. It owns escaping, template loading, nested context
objects and resource lifecycle, just as a caller selecting HTMLResponse owns
the HTML it supplies. Synchronous rendering runs inline and must be bounded and
nonblocking; preloading/caching remains caller-owned.

`JinjaTemplates(environment)` accepts a caller-installed Jinja-compatible
environment without importing or installing an engine. It requires explicit
autoescaping, checks the selected template name and environment ownership, and
uses render/render_async according to the environment. Keep engine configuration
stable after first template loading. Missing templates and renderer failures are
application errors; the ordinary redacted error boundary handles them. No
Pretty Notebook template or application factory is changed.

With Expose API and Jinja already installed in a selected environment, run
`python scripts/check_jinja.py` to qualify synchronous/asynchronous escaping and
local URLs. Core CI uses a structural engine fixture and does not install Jinja.
The adapter follows Jinja's [environment, rendering and autoescape interface](https://jinja.palletsprojects.com/en/stable/api/).

## Installed composition acceptance

`examples/http_extensions.py` combines an escaped caller-owned renderer, named
static mounts, conditional downloads, a byte stream, a bounded authenticated
upload receipt and explicit route documentation. It consumes caller-selected
assets containing site.css/download.txt. Its default authenticator denies every
upload; callers provide verification. Uploads are hashed inside their owned
context, then discarded rather than published or persisted.

The framework integration tests exercise mounted root paths and real HTTP,
including HEAD/ranges, pre-body authentication denial, multipart cleanup,
stream closure and lifespan shutdown. The same base suite runs once per clean
wheel/source installation. Distribution receipts retain its HTTP module counts,
platform/version, pass/failure and skip details, and confirm that Jinja was not
imported. Existing three-platform CI retains those receipts alongside its
optional persistence/validation qualification. The separate caller-installed
Jinja probe records engine evidence without adding a package requirement.

This qualifies the selected framework profiles. Pretty Notebook's dependencies,
protocol parity, application factories, transactions and deployment acceptance
remain owned by PNB-WEB-01 through PNB-WEB-04; none begins in these batches.
