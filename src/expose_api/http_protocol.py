"""Shared HTTP event and failure boundaries."""


class ProtocolError(RuntimeError):
    """The ASGI server supplied an invalid protocol event."""


class _Disconnected(Exception):
    pass


class _HTTPError(Exception):
    def __init__(self, status, code, message, *, headers=()):
        self.status, self.code, self.message, self.headers = status, code, message, headers
