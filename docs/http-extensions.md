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
