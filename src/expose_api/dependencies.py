"""Explicit, typed request providers with request-owned resource lifetimes."""

import asyncio
import inspect
from contextlib import AsyncExitStack
from contextvars import ContextVar

from .errors import ConfigurationError
from .http_protocol import _settle_task

_resolving = ContextVar('expose_api_resolving', default=())


def _provider(key, provider):
    if not isinstance(key, type) or not callable(provider):
        raise ConfigurationError('Providers require a concrete type and a request callable.')


class DependencyScope:
    def __init__(self, request, providers):
        self.request, self.providers = request, dict(providers)
        self.tasks, self.waiting = {}, {}
        self.stack = AsyncExitStack()
        self.closed = False

    async def resolve(self, key):
        if self.closed or key not in self.providers:
            raise ConfigurationError('Dependency scope is closed or the type has no provider.')
        path = _resolving.get()
        # Paths include the scope, so a provider can safely call a separate app.
        current = [kind for scope, kind in path if scope is self]
        if key in current:
            raise ConfigurationError('Dependency cycle detected.')
        task = self.tasks.get(key)
        parent = current[-1] if current else None
        if task is not None and not task.done() and parent is not None:
            pending, seen = [key], set()
            while pending:
                node = pending.pop()
                if node is parent:
                    raise ConfigurationError('Dependency cycle detected.')
                if node not in seen:
                    seen.add(node)
                    pending.extend(self.waiting.get(node, ()))
        if task is None:
            task = self.tasks[key] = asyncio.create_task(self._build(key))
        if parent is not None:
            edges = self.waiting.setdefault(parent, {})
            edges[key] = edges.get(key, 0) + 1
        try:
            return await asyncio.shield(task)
        finally:
            if parent is not None:
                edges[key] -= 1
                if not edges[key]:
                    del edges[key]

    async def _build(self, key):
        token = _resolving.set((*_resolving.get(), (self, key)))
        try:
            value = self.providers[key](self.request)
            if inspect.isawaitable(value):
                value = await value
            if hasattr(value, '__aenter__') and hasattr(value, '__aexit__'):
                value = await self.stack.enter_async_context(value)
            elif hasattr(value, '__enter__') and hasattr(value, '__exit__'):
                value = self.stack.enter_context(value)
            if not isinstance(value, key):
                raise ConfigurationError('A provider returned a value of the wrong type.')
            return value
        finally:
            _resolving.reset(token)

    async def close(self, error=None):
        if self.closed:
            return
        self.closed = True
        async def cleanup():
            for task in self.tasks.values():
                if not task.done():
                    task.cancel()
            await asyncio.gather(*self.tasks.values(), return_exceptions=True)
            # Context managers see the request error, but cannot suppress it.
            await self.stack.__aexit__(type(error) if error else None, error,
                                       error.__traceback__ if error else None)
        task = asyncio.create_task(cleanup())
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            await _settle_task(task)
            raise
