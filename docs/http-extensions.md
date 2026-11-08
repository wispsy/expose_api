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
