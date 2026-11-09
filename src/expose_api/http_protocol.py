"""Shared HTTP event and failure boundaries."""


class ProtocolError(RuntimeError):
    """The ASGI server supplied an invalid protocol event."""


class _Disconnected(Exception):
    pass


class _HTTPError(Exception):
    def __init__(self, status, code, message, *, headers=()):
        self.status, self.code, self.message, self.headers = status, code, message, headers


def _request_event(event):
    """Validate server-owned HTTP receive events without converting their errors."""
    if not isinstance(event, dict):
        raise ProtocolError('Expected an HTTP request or disconnect event.')
    kind = event.get('type')
    if kind == 'http.disconnect':
        return kind
    if (kind != 'http.request' or type(event.get('body', b'')) is not bytes
            or type(event.get('more_body', False)) is not bool):
        raise ProtocolError('Expected an HTTP request or disconnect event.')
    return kind
