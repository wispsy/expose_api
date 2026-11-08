"""Native ASGI 3 application with explicit dataclass resource routes."""

import inspect
import json
import logging
import math
import re
import sys
from collections.abc import Callable, Iterable
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass, replace
from urllib.parse import parse_qsl, quote

from .access import Principal
from .errors import AccessDeniedError, AccessScanError, ConfigurationError, ConflictError, NotFoundError, RevisionConflictError, ValidationError
from .models import MAX_DEPTH
from .resources import Resource
from .websocket import EventSockets
from .http_protocol import ProtocolError, _Disconnected, _HTTPError
from .requests import HTTPRequest
from .responses import Response, HTTPException
from .routing import Router, make_route, prefixed_routes, _prefix
from .composition import MountedResponse, mounted_response

logger = logging.getLogger("expose_api")
OPERATIONS = frozenset({"list", "get", "create", "update", "delete"})


@dataclass(frozen=True, slots=True)
class Request:
    """Authorization context; identity is a canonical key, not a fetched row."""

    scope: dict
    model: type
    operation: str
    identity: str | None
    principal: Principal | None = None

    def header(self, name: str) -> str | None:
        values = [v.decode("latin-1") for k, v in self.scope.get("headers", ()) if k.lower() == name.lower().encode("ascii")]
        return values[0] if len(values) == 1 else None


@dataclass(frozen=True, slots=True)
class _Mount:
    path: str
    resource: Resource
    operations: frozenset[str]
    authorize: Callable | None
    record_policy: Callable | None
    scan_limit: int


@dataclass(slots=True)
class _RequestState:
    protected: bool = False
    context: HTTPRequest | None = None


@asynccontextmanager
async def _empty_lifespan(app):
    yield


def _response(status, data=None, headers=()):
    if status == 204:
        return status, list(headers), b""
    body = json.dumps(data, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
    return status, [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode()), *headers], body


def _error(status, code, message, *, details=(), headers=()):
    return _response(status, {"error": {"code": code, "message": message, "details": list(details)}}, headers)


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key.")
        result[key] = value
    return result


def _constant(value):
    raise ValueError("Non-finite JSON number.")


def _check_json(value):
    pending = [(value, 0)]
    while pending:
        node, depth = pending.pop()
        if depth > MAX_DEPTH or type(node) is float and not math.isfinite(node):
            raise ValueError("Invalid JSON depth or number.")
        if isinstance(node, dict):
            pending.extend((v, depth + 1) for v in node.values())
        elif isinstance(node, list):
            pending.extend((v, depth + 1) for v in node)


class App:
    """A dependency-free ASGI app. Include resources before the first ASGI call."""

    def __init__(self, *, title: str = "Expose API", max_body_size: int = 1024 * 1024, lifespan=None,
                 authenticate=None, auth_challenge: str | None = None):
        if type(max_body_size) is not int or max_body_size < 1:
            raise ConfigurationError("max_body_size must be a positive integer.")
        if authenticate is not None:
            if (not callable(authenticate) or type(auth_challenge) is not str or len(auth_challenge) > 512
                    or not re.fullmatch(r"[A-Za-z][A-Za-z0-9!#$%&'*+.^_`|~-]*(?: [ -~]+)?", auth_challenge)):
                raise ConfigurationError("Authentication requires a callback and a bounded ASCII challenge.")
        elif auth_challenge is not None:
            raise ConfigurationError("An authentication challenge requires a callback.")
        self.authenticate, self.auth_challenge = authenticate, auth_challenge
        self.title, self.max_body_size = title, max_body_size
        self.state: dict = {}
        self._lifespan_factory = lifespan or _empty_lifespan
        self._mounts: list[_Mount] = []
        self._frozen = False
        self._sockets = EventSockets(self)
        self._tables = {}
        self._router = Router()
        self._applications = []
        self._middleware = []
        self._asgi_application = None
        self._exception_handlers = {}
        self._security_schemes = {}

    def openapi_security_scheme(self, name, definition):
        """Register descriptive security metadata without installing authentication."""
        from .route_docs import security_scheme
        if self._frozen:
            raise ConfigurationError("Register security schemes before serving.")
        selected = security_scheme(name, definition)
        if name in self._security_schemes:
            raise ConfigurationError("Register security scheme names once before serving.")
        self._security_schemes[name] = selected

    def exception_handler(self, exception_type):
        """Register a request/exception handler returning an explicit Response."""
        def register(handler):
            if (self._frozen or not isinstance(exception_type, type)
                    or not issubclass(exception_type, Exception) or issubclass(exception_type, ProtocolError)
                    or not callable(handler) or exception_type in self._exception_handlers):
                raise ConfigurationError("Register each HTTP exception type once before serving.")
            self._exception_handlers[exception_type] = handler
            return handler
        return register

    def add_middleware(self, factory, **options):
        """Register an ASGI factory; first registered is outermost."""
        if self._frozen or not callable(factory):
            raise ConfigurationError("Register callable middleware factories before serving.")
        self._middleware.append((factory, dict(options)))

    def mount(self, path, application):
        """Mount an HTTP ASGI application; the parent alone owns lifespan."""
        _prefix(path)
        if self._frozen or not path or not callable(application):
            raise ConfigurationError("Mount callable applications on nonempty prefixes before serving.")
        prefixes = [mount.path for mount in self._mounts] + [prefix for prefix, _ in self._applications]
        if (any(path == prefix or path.startswith(prefix + "/") or prefix.startswith(path + "/")
                for prefix in prefixes) or any(route.overlaps(path) for route in self._router._routes)):
            raise ConfigurationError("Application mounts cannot overlap registered namespaces.")
        self._applications.append((path, application))

    def route(self, path, *, methods=("GET",), public=False, authorize=None, name=None,
              authenticate=False, dependencies=(), docs=None):
        """Register a handler receiving one HTTPRequest and returning a Response."""
        def register(handler):
            if self._frozen:
                raise ConfigurationError("Register routes before serving the application.")
            route = make_route(path, handler, methods=methods, public=public, authorize=authorize, name=name,
                               authenticate=authenticate, dependencies=dependencies, docs=docs)
            if route.authenticate and self.authenticate is None:
                raise ConfigurationError("Authenticated routes require an application authenticator.")
            if (any(route.overlaps(mount.path) for mount in self._mounts)
                    or any(route.overlaps(prefix) for prefix, _ in self._applications)):
                raise ConfigurationError("Custom routes cannot overlap resource namespaces.")
            self._router._add(route)
            return handler
        return register

    def include_router(self, router, *, prefix="", name_prefix=""):
        """Snapshot router registrations atomically under a static prefix."""
        if self._frozen:
            raise ConfigurationError("Include routers before serving the application.")
        routes = prefixed_routes(router, prefix, name_prefix)
        if any(route.authenticate for route in routes) and self.authenticate is None:
            raise ConfigurationError("Authenticated routes require an application authenticator.")
        if (any(route.overlaps(mount.path) for route in routes for mount in self._mounts)
                or any(route.overlaps(prefix) for route in routes for prefix, _ in self._applications)):
            raise ConfigurationError("Custom routes cannot overlap resource namespaces.")
        self._router._extend(routes)

    def url_path_for(self, name, /, **params):
        return self._router.url_path_for(name, **params)

    def tabular(self, resource, **options):
        """Opt into bounded registered tables and explicitly writable columns."""
        from .tabular import TabularProfile
        mount = next((mount for mount in self._mounts if mount.resource is resource), None)
        if self._frozen or mount is None or mount.path in self._tables:
            raise ConfigurationError("Register one tabular profile on an included resource before serving.")
        profile = TabularProfile(mount, **options)
        self._tables[mount.path] = profile
        return profile

    def subscribe(self, resource, **options):
        """Explicitly enable bounded, protected single-record WebSockets."""
        if self._frozen:
            raise ConfigurationError("Register subscriptions before serving the application.")
        mount = next((m for m in self._mounts if m.resource is resource), None)
        if mount is None:
            raise ConfigurationError("Subscribe to a resource registered on this App.")
        self._sockets.register(mount, options)

    def run(self, *, host: str = "127.0.0.1", port: int = 8000) -> None:
        """Run the standard-library development HTTP server until interrupted."""
        from .server import run
        run(self, host=host, port=port)

    def openapi(self, *, version: str = "0.1.0", description: str | None = None) -> dict:
        """Generate a detached description of explicitly registered resources."""
        from .openapi import document
        return document(self, version=version, description=description)

    def include[T](self, model: type[T], *, store, path: str | None = None, id_field: str = "id",
                   operations: Iterable[str] = OPERATIONS, public: bool = False, authorize=None,
                   record_policy=None, scan_limit: int = 10000, schema=None) -> Resource[T]:
        if self._frozen:
            raise ConfigurationError("Register resources before serving the application.")
        if type(public) is not bool or (public and authorize is not None) or (not public and not callable(authorize)):
            raise ConfigurationError("Choose public=True or an authorization callback.")
        resource = Resource(model, store=store, id_field=id_field, schema=schema)
        if record_policy is not None:
            if public or self.authenticate is None:
                raise ConfigurationError("Record policies require explicit authentication and operation authorization.")
            resource.for_principal(Principal("configuration"), record_policy, scan_limit=scan_limit)
        if path is None:
            path = "/" + re.sub(r"(?<!^)(?=[A-Z])", "_", model.__name__).lower() + "s"
        if type(path) is not str or not re.fullmatch(r"/[A-Za-z0-9_-]+(?:/[A-Za-z0-9_-]+)*", path):
            raise ConfigurationError("Resource paths must contain static URL segments without a trailing slash.")
        if isinstance(operations, str):
            raise ConfigurationError("Operations must be a collection of operation names.")
        selected = frozenset(operations)
        if not selected or selected - OPERATIONS:
            raise ConfigurationError("Select supported CRUD operations.")
        for mount in self._mounts:
            if mount.resource.model is model or mount.resource.store is store:
                raise ConfigurationError("Each resource requires its own model and store.")
            if path == mount.path or path.startswith(mount.path + "/") or mount.path.startswith(path + "/"):
                raise ConfigurationError("Resource paths cannot overlap.")
        if any(route.overlaps(path) for route in self._router._routes):
            raise ConfigurationError("Resource namespaces cannot overlap custom routes.")
        if any(path == prefix or path.startswith(prefix + "/") or prefix.startswith(path + "/")
               for prefix, _ in self._applications):
            raise ConfigurationError("Resources cannot overlap application mounts.")
        self._mounts.append(_Mount(path, resource, selected, authorize, record_policy, scan_limit))
        return resource

    async def __call__(self, scope, receive, send):
        self._frozen = True
        if self._asgi_application is None:
            application = self._serve
            for factory, options in reversed(self._middleware):
                application = factory(application, **options)
                if not callable(application):
                    raise ConfigurationError("Middleware factories must return an ASGI callable.")
            self._asgi_application = application
        if scope["type"] != "http":
            await self._asgi_application(scope, receive, send)
            return
        scope = dict(scope)
        scope["headers"] = tuple(scope.get("headers", ()))
        state = _RequestState()
        scope["expose_api.request_state"] = state
        started = False
        async def guarded_send(event):
            nonlocal started
            if event["type"] == "http.response.start":
                if started:
                    raise ProtocolError("Response already started.")
                if state.protected:
                    headers = [(key, value) for key, value in event.get("headers", ()) if key.lower() != b"cache-control"]
                    event = {**event, "headers": [*headers, (b"cache-control", b"no-store")]}
                started = True
            elif event["type"] == "http.response.body":
                if not started:
                    raise ProtocolError("Response body precedes response start.")
                if scope["method"] == "HEAD":
                    event = {**event, "body": b""}
            await send(event)
        try:
            await self._asgi_application(scope, receive, guarded_send)
        except _Disconnected:
            return
        except ProtocolError:
            raise
        except Exception as exc:
            if started:
                raise
            response = await self._exception_response(exc, scope, receive, state)
            if isinstance(response, Response):
                await response(scope, receive, guarded_send)
                return
            status, headers, body = response
            await guarded_send({"type": "http.response.start", "status": status, "headers": headers})
            await guarded_send({"type": "http.response.body", "body": body})

    async def _serve(self, scope, receive, send):
        if scope["type"] == "lifespan":
            await self._lifespan(receive, send)
            return
        if scope["type"] == "websocket":
            await self._sockets(scope, receive, send)
            return
        if scope["type"] != "http":
            raise ProtocolError("Unsupported ASGI scope type.")
        scope = dict(scope)
        scope["headers"] = tuple(scope.get("headers", ()))
        state = scope["expose_api.request_state"]
        try:
            response = await self._dispatch(scope, receive, state)
            if isinstance(response, Response):
                response = await response.prepare(scope)
                if not isinstance(response, Response):
                    raise ConfigurationError("Response preparation must return a Response.")
        except _Disconnected:
            return
        except ProtocolError:
            raise
        except Exception as exc:
            response = await self._exception_response(exc, scope, receive, state)
        if isinstance(response, MountedResponse):
            await response.application(response.scope, receive, send)
            return
        if isinstance(response, Response):
            await response(scope, receive, send)
            return
        status, headers, body = response
        await send({"type": "http.response.start", "status": status, "headers": headers})
        await send({"type": "http.response.body", "body": b"" if scope["method"] == "HEAD" else body})

    async def _exception_response(self, exc, scope, receive, state):
        handler = next((self._exception_handlers[kind] for kind in type(exc).__mro__
                        if kind in self._exception_handlers), None)
        if handler is not None:
            context = state.context
            if context is None:
                context = HTTPRequest(scope, receive, max_body_size=self.max_body_size, app=self)
                context._failure = ProtocolError("Exception handlers cannot read an unowned request body.")
            try:
                response = handler(context, exc)
                if inspect.isawaitable(response):
                    response = await response
                if not isinstance(response, Response):
                    raise ConfigurationError("Exception handlers must return an explicit Response.")
                response = await response.prepare(scope)
                if not isinstance(response, Response):
                    raise ConfigurationError("Response preparation must return a Response.")
                return response
            except (ProtocolError, _Disconnected):
                raise
            except Exception as failure:
                logger.error("HTTP exception handler failed (%s).", type(failure).__name__)
                return _error(500, "internal", "Internal server error.")
        if isinstance(exc, _HTTPError):
            return _error(exc.status, exc.code, exc.message, headers=exc.headers)
        if isinstance(exc, HTTPException):
            return _error(exc.status, "http_error", exc.detail, headers=exc.headers)
        if isinstance(exc, ValidationError):
            return _error(422, "validation", "Data does not match the model.",
                          details=[issue.as_dict() for issue in exc.issues])
        for kind, status, code, message in (
            (AccessDeniedError, 403, "forbidden", "Operation not permitted."),
            (AccessScanError, 503, "access_scan", "Visible page exceeds the scan budget."),
            (NotFoundError, 404, "not_found", "Record not found."),
            (RevisionConflictError, 409, "revision_conflict", "Record changed after the expected revision."),
            (ConflictError, 409, "conflict", "Record already exists."),
        ):
            if isinstance(exc, kind):
                return _error(status, code, message)
        logger.error("Unhandled application error (%s).", type(exc).__name__)
        return _error(500, "internal", "Internal server error.")

    async def _dispatch(self, scope, receive, state):
        path, root = scope["path"], scope.get("root_path", "").rstrip("/")
        if root and (path == root or path.startswith(root + "/")):
            path = path[len(root):] or "/"
        for prefix, application in self._applications:
            if path == prefix or path.startswith(prefix + "/"):
                return mounted_response(application, scope, path, root, prefix)
        matches = [(route, params) for route in self._router._routes
                   if (params := route.match(path)) is not None]
        if matches:
            return await self._dispatch_route(matches, scope, receive, state)
        for prefix, profile in self._tables.items():
            if path.startswith(prefix + "/_table/"):
                return await profile.dispatch(self, path[len(prefix + "/_table/"):], scope, receive, state)
        for mount in self._mounts:
            if path == mount.path:
                identity = None
                choices = {"GET": "list", "POST": "create"}
                break
            if path.startswith(mount.path + "/") and "/" not in path[len(mount.path) + 1:]:
                identity = path[len(mount.path) + 1:]
                if not identity:
                    continue
                choices = {"GET": "get", "PATCH": "update", "DELETE": "delete"}
                break
        else:
            raise _HTTPError(404, "not_found", "Route not found.")
        state.protected = mount.record_policy is not None
        enabled = {method: operation for method, operation in choices.items() if operation in mount.operations}
        if not enabled:
            raise _HTTPError(404, "not_found", "Route not found.")
        allowed = sorted({*enabled, "OPTIONS", *({"HEAD"} if "GET" in enabled else set())})
        allow = ((b"allow", ", ".join(allowed).encode()),)
        method = scope["method"]
        if method == "OPTIONS":
            return _response(204, headers=allow)
        operation = enabled.get("GET" if method == "HEAD" else method)
        if operation is None:
            raise _HTTPError(405, "method_not_allowed", "Method not allowed.", headers=allow)
        if identity is not None:
            try:
                identity = mount.resource.schema.parse_id(identity)
            except ValidationError:
                raise _HTTPError(400, "identity", "Invalid record identity.") from None
        context = await self._context(mount, scope, operation, identity)
        pagination = self._query(scope.get("query_string", b""), operation)
        resource = mount.resource
        if mount.record_policy is not None:
            resource = resource.for_principal(context.principal, mount.record_policy, scan_limit=mount.scan_limit)
        if operation == "list":
            values = await resource.list(**pagination)
            return _response(200, [self._dump(resource, value) for value in values])
        if operation == "get":
            return _response(200, self._dump(resource, await resource.get(identity)))
        if operation == "delete":
            await resource.delete(identity)
            return _response(204)
        data = await self._body(scope, receive)
        if operation == "create":
            value = await resource.create(data)
            location = quote(root + mount.path, safe="/") + "/" + quote(resource.schema.key(value), safe="")
            return _response(201, self._dump(resource, value), ((b"location", location.encode("ascii")),))
        return _response(200, self._dump(resource, await resource.update(identity, data)))

    async def _dispatch_route(self, matches, scope, receive, state):
        method = scope["method"]
        state.protected = any(route.authorize is not None for route, _ in matches)
        allowed = {"OPTIONS"}.union(*(route.methods for route, _ in matches))
        if "GET" in allowed:
            allowed.add("HEAD")
        allow = ((b"allow", ", ".join(sorted(allowed)).encode("ascii")),)
        selected = next(((route, params) for route, params in matches if method in route.methods), None)
        if selected is None and method == "HEAD":
            selected = next(((route, params) for route, params in matches if "GET" in route.methods), None)
        if selected is None:
            if method == "OPTIONS":
                return _response(204, headers=allow)
            raise _HTTPError(405, "method_not_allowed", "Method not allowed.", headers=allow)
        route, params = selected
        state.protected = route.authorize is not None
        context = HTTPRequest(scope, receive, max_body_size=self.max_body_size, path_params=params, app=self)
        state.context = context
        if route.authenticate:
            principal = self.authenticate(context)
            if inspect.isawaitable(principal):
                principal = await principal
            if principal is None:
                raise _HTTPError(401, "unauthenticated", "Authentication required.",
                                 headers=((b"www-authenticate", self.auth_challenge.encode("ascii")),))
            if type(principal) is not Principal:
                raise ConfigurationError("Authentication must return a Principal or None.")
            context.principal = principal
        if route.authorize is not None:
            permitted = route.authorize(context)
            if inspect.isawaitable(permitted):
                permitted = await permitted
            if type(permitted) is not bool:
                raise ConfigurationError("Authorization callbacks must return a boolean.")
            if not permitted:
                raise _HTTPError(403, "forbidden", "Operation not permitted.")
        for dependency in route.dependencies:
            result = dependency(context)
            if inspect.isawaitable(result):
                result = await result
            if result is not None:
                raise ConfigurationError("Request dependencies must return None; raise HTTPException to deny.")
        response = route.handler(context)
        if inspect.isawaitable(response):
            response = await response
        if not isinstance(response, Response):
            raise ConfigurationError("HTTP handlers must return an explicit Response.")
        return response

    async def _context(self, mount, scope, operation, identity):
        context = Request(dict(scope), mount.resource.model, operation, identity)
        if mount.record_policy is not None:
            principal = self.authenticate(context)
            if inspect.isawaitable(principal):
                principal = await principal
            if principal is None:
                raise _HTTPError(401, "unauthenticated", "Authentication required.",
                                 headers=((b"www-authenticate", self.auth_challenge.encode("ascii")),))
            if type(principal) is not Principal:
                raise ConfigurationError("Authentication must return a Principal or None.")
            context = replace(context, principal=principal)
        if mount.authorize is not None:
            permitted = mount.authorize(context)
            if inspect.isawaitable(permitted):
                permitted = await permitted
            if type(permitted) is not bool:
                raise ConfigurationError("Authorization callbacks must return a boolean.")
            if not permitted:
                raise _HTTPError(403, "forbidden", "Operation not permitted.")
        return context

    @staticmethod
    def _dump(resource, value):
        try:
            return resource.schema.dump(value)
        except ValidationError:
            raise RuntimeError("Stored record does not match its model.") from None

    @staticmethod
    def _query(raw, operation):
        try:
            if len(raw) > 8192 or re.search(rb"%(?![0-9a-fA-F]{2})", raw):
                raise ValueError
            pairs = parse_qsl(raw.decode("ascii"), keep_blank_values=True, strict_parsing=True,
                              encoding="utf-8", errors="strict", max_num_fields=16)
            values = {}
            for key, value in pairs:
                if operation != "list" or key not in ("offset", "limit") or key in values:
                    raise ValueError
                if not re.fullmatch(r"[0-9]{1,10}", value):
                    raise ValueError
                values[key] = int(value)
            if values.get("offset", 0) < 0 or not 1 <= values.get("limit", 100) <= 1000:
                raise ValueError
            return values
        except (ValueError, UnicodeError):
            raise _HTTPError(400, "query", "Invalid query parameters.") from None

    async def _body(self, scope, receive, *, max_size=None):
        max_size = self.max_body_size if max_size is None else min(self.max_body_size, max_size)
        return await HTTPRequest(scope, receive, max_body_size=max_size).json()

    async def _lifespan(self, receive, send):
        event = await receive()
        if event["type"] != "lifespan.startup":
            raise ProtocolError("Expected lifespan.startup.")
        stack = AsyncExitStack()
        self._sockets.stopping = False
        self._sockets.owner = None
        try:
            await stack.enter_async_context(self._lifespan_factory(self))
        except Exception as exc:
            logger.error("Application startup failed (%s).", type(exc).__name__)
            await send({"type": "lifespan.startup.failed", "message": "Application startup failed."})
            return
        try:
            await send({"type": "lifespan.startup.complete"})
            event = await receive()
            if event["type"] != "lifespan.shutdown":
                raise ProtocolError("Expected lifespan.shutdown.")
        except BaseException:
            await self._sockets.stop()
            await stack.__aexit__(*sys.exc_info())
            raise
        try:
            await self._sockets.stop()
            await stack.aclose()
        except Exception as exc:
            logger.error("Application shutdown failed (%s).", type(exc).__name__)
            await send({"type": "lifespan.shutdown.failed", "message": "Application shutdown failed."})
        else:
            await send({"type": "lifespan.shutdown.complete"})
