"""Explicit, typed request providers with request-owned resource lifetimes."""

import asyncio
import inspect
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
        self.tasks, self.results, self.waiting = {}, {}, {}
        self.contexts = []
        self._closing_error = None
        self.closed = False

    async def resolve(self, key):
        if self.closed or key not in self.providers:
            raise ConfigurationError('Dependency scope is closed or the type has no provider.')
        path = _resolving.get()
        # Paths include the scope, so a provider can safely call a separate app.
        current = [kind for scope, kind in path if scope is self]
        if key in current:
            raise ConfigurationError('Dependency cycle detected.')
        result = self.results.get(key)
        parent = current[-1] if current else None
        if result is not None and not result.done() and parent is not None:
            pending, seen = [key], set()
            while pending:
                node = pending.pop()
                if node is parent:
                    raise ConfigurationError('Dependency cycle detected.')
                if node not in seen:
                    seen.add(node)
                    pending.extend(self.waiting.get(node, ()))
        if result is None:
            result = self.results[key] = asyncio.get_running_loop().create_future()
            self.tasks[key] = asyncio.create_task(self._build(key, result))
        if parent is not None:
            edges = self.waiting.setdefault(parent, {})
            edges[key] = edges.get(key, 0) + 1
        try:
            return await asyncio.shield(result)
        finally:
            if parent is not None:
                edges[key] -= 1
                if not edges[key]:
                    del edges[key]

    async def _publish(self, key, value, result, *, managed=False):
        if not isinstance(value, key):
            raise ConfigurationError('A provider returned a value of the wrong type.')
        if managed:
            closing = asyncio.get_running_loop().create_future()
            self.contexts.append((asyncio.current_task(), closing))
        result.set_result(value)
        if managed:
            error = await closing
            if error is not None:
                raise error

    async def _build(self, key, result):
        token = _resolving.set((*_resolving.get(), (self, key)))
        try:
            value = self.providers[key](self.request)
            if inspect.isawaitable(value):
                value = await value
            if hasattr(value, '__aenter__') and hasattr(value, '__aexit__'):
                async with value as service:
                    await self._publish(key, service, result, managed=True)
            elif hasattr(value, '__enter__') and hasattr(value, '__exit__'):
                with value as service:
                    await self._publish(key, service, result, managed=True)
            else:
                await self._publish(key, value, result)
        except BaseException as error:
            if not result.done():
                if isinstance(error, asyncio.CancelledError):
                    result.cancel()
                else:
                    result.set_exception(error)
            elif error is not self._closing_error:
                raise
        finally:
            if not result.done():
                result.set_exception(ConfigurationError('A provider did not publish a service value.'))
            _resolving.reset(token)

    async def close(self, error=None):
        if self.closed:
            return
        self.closed = True
        async def cleanup():
            pending = [task for key, task in self.tasks.items() if not self.results[key].done()]
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            failure, current = None, error
            for task, closing in reversed(self.contexts):
                self._closing_error = current
                if not closing.done():
                    closing.set_result(current)
                try:
                    await task
                except BaseException as exc:
                    failure = current = exc
            await asyncio.gather(*self.tasks.values(), return_exceptions=True)
            for result in self.results.values():
                if result.done() and not result.cancelled():
                    result.exception()
            if failure is not None:
                raise failure
        task = asyncio.create_task(cleanup())
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            await _settle_task(task)
            raise
