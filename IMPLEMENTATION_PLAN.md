# HTTP application enhancement plan

Adopted: 2026-11-08.

## Goal and evidence

Make `expose_api` capable of hosting Pretty Notebook's web application through
explicit adapters and a tested migration. Preserve the existing dataclass
resource API while adding a separate general HTTP application layer. Success
means preserving the application's observable protocol and persistence behavior.
FastAPI import compatibility is outside this contract.

The supplied **Assess Replacement Readiness** recommends general routing,
request/response primitives, composition and a staged Pretty Notebook pilot.
The inspected baselines are Expose API `cdff4682db5decc3d6bdb55f7ba5a6a2105f154a`
and Pretty Notebook `24b8988bb8b1801f21c462a4f9ffd2112aa9be00`. The latter's local
checkout matches its remote main at assessment time. Its application factory
uses custom routes, trusted-host/security/audit middleware, static mounts,
templates, forms, uploads, caller authentication and permission dependencies.
Its checked publishing protocol already has integration tests. Those tests,
rather than a route-count estimate, define the eventual migration gate.

The foundation already provides native ASGI/lifespan, strict dataclass JSON
resources, generated OpenAPI, operation/record policies, optional persistence,
tabular requests and protected record events. Reuse those contracts. The new
layer must preserve their default behavior and dependency-free imports.

## Architecture and adopted contracts

### Separate resource and handler interfaces

`App.include()` continues to register typed CRUD resources. `App.route()` and
`Router.route()` register explicit request handlers. `HTTPRequest` is the general
HTTP request; the existing exported `Request` remains the resource authorization
context. Handlers receive one request argument and return an explicit Response.
Await asynchronous results; synchronous handlers run inline and must be short
and nonblocking. No automatic annotation injection, thread pool, ORM, credential
provider or Pydantic emulation is inferred.

Register resources, routes, routers, middleware, exception handlers and mounts
before first ASGI use. Inclusion snapshots router registrations. Preserve
registration order; ambiguous duplicate method/path patterns fail early.
Resources reserve their URL namespace, including tabular/event extensions;
custom handlers and application mounts cannot intercept it.

Support static segments and named `str`, nonnegative `int`, UUID and final
`path` converters. Match decoded ASGI paths once. No trailing-slash redirects.
HEAD uses explicit HEAD if registered, otherwise GET semantics with no body.
OPTIONS reports the union of methods for matching routes; 405 includes Allow.
Names are unique and URL reversal produces local root-path-aware URLs with
validated, once-encoded parameters. Route order is application configuration.

### Bounded request and response behavior

Responses own bytes, status, ordered repeated headers and explicit media type.
Validate status and prevent header/cookie injection before response-start.
JSON serialization rejects non-finite values. HTML, redirects, cookies and
binary responses do not require a third-party framework. A public HTTPException
contains intentionally public detail; unexpected exception text stays private.
HEAD retains representation headers while suppressing bytes; 204/304 forbid
payloads. Send failures propagate without sending a second response.

Requests expose repeated headers/query fields, cookies, state and converted
path parameters. Body readers enforce actual-byte and Content-Length bounds,
strict event framing, UTF-8, JSON media/duplicate-key/depth rules, and bounded
urlencoded forms. Body caching permits repeated reads after successful complete
consumption. Interrupted or failed reads cannot silently restart consumption.
Disconnects and cancellation stop processing. Multipart is a separate parser
selection with explicit total, field, part and spooling limits.

### Composition and access

Middleware uses the ordinary ASGI callable interface. Factories wrap in declared
outer-to-inner order and are constructed once before serving. The parent owns
lifespan and middleware cleanup; mounts dispatch HTTP with adjusted root_path
and copied scope. Mounts match whole segments and do not imply child lifespan,
static-file serving or WebSocket routing.

Custom routes require `public=True` or an authorization callback. Opt-in
authentication reuses the caller-owned authenticator and verified Principal;
authorization and ordered explicit request dependencies run before body reads.
These dependencies are request hooks, not FastAPI signature dependency injection.
Protected responses and errors receive no-store. Registered exception handlers
select the closest matching exception type; handler failures use the sanitized
default error boundary. Cancellation, protocol errors and transport errors retain
their ownership and must not be converted into successful responses.

### Optional integrations and migration

Keep zero mandatory runtime dependencies. Do not adopt Starlette, Jinja2 or a
multipart parser merely for convenience; select optional integrations with
their own contract and approval before adding requirements. Pretty Notebook
may retain its existing template, Pydantic, ORM and authentication packages
while its routes move to the explicit handler interface. The web runtime's
Python 3.14 floor and the library's wider supported versions are separate
compatibility questions.

Do not switch the default Pretty Notebook factory until parity evidence passes.
The migration order is health, search, authenticated publishing, then remaining
API/page/form groups. Preserve status codes, response/error shape, headers,
cookies, redirects, root paths, validation ordering, permissions, revisions,
ETags, idempotency, upload limits, transaction rollback, logs and worker visibility.

## Canonical queue and PR boundaries

[Wispsy Expose API Project 7](https://github.com/orgs/wispsy/projects/7) holds the
issues, dependencies and current status. Migration issues initially live in this
queue; implementation changes belong in `pretty-notebook` when their gates open.
Unrelated optional CLI and capability adapters remain deferred.

| Task | Scope / acceptance boundary | Dependencies |
| --- | --- | --- |
| [WEB-00](https://github.com/wispsy/expose_api/issues/75) | Adopt this plan and linked Project queue | None |
| [WEB-01](https://github.com/wispsy/expose_api/issues/76) | Explicit response/status/header/cookie/error primitives | [WEB-00](https://github.com/wispsy/expose_api/issues/75) |
| [WEB-02](https://github.com/wispsy/expose_api/issues/77) | Bounded request, repeated query, JSON and urlencoded form readers | [WEB-01](https://github.com/wispsy/expose_api/issues/76) |
| [WEB-03](https://github.com/wispsy/expose_api/issues/78) | General HTTP Router and App dispatch with resource regressions | [WEB-02](https://github.com/wispsy/expose_api/issues/77) |
| [WEB-04](https://github.com/wispsy/expose_api/issues/79) | Router snapshots, nested prefixes and named local URLs | [WEB-03](https://github.com/wispsy/expose_api/issues/78) |
| [WEB-05](https://github.com/wispsy/expose_api/issues/80) | ASGI middleware and segment-boundary HTTP mounts | [WEB-04](https://github.com/wispsy/expose_api/issues/79) |
| [WEB-06](https://github.com/wispsy/expose_api/issues/81) | Authentication, explicit dependencies, exception handlers, examples | [WEB-05](https://github.com/wispsy/expose_api/issues/80) |
| [WEB-07](https://github.com/wispsy/expose_api/issues/82) | Streaming/file ownership, conditional and range responses | [WEB-06](https://github.com/wispsy/expose_api/issues/81) |
| [WEB-08](https://github.com/wispsy/expose_api/issues/83) | Multipart/parser decision, bounded uploads and cleanup | WEB-02, WEB-06; parser decision |
| [WEB-09](https://github.com/wispsy/expose_api/issues/84) | Explicit custom-route OpenAPI metadata and security | WEB-04, WEB-06 |
| [WEB-10](https://github.com/wispsy/expose_api/issues/85) | Qualified optional templates and rooted static content | WEB-05, WEB-07; adapter decision |
| [PNB-WEB-01](https://github.com/wispsy/expose_api/issues/86) | Dual-factory compatibility inventory and unchanged protocol fixtures | WEB-06, WEB-08, WEB-09, WEB-10 |
| [PNB-WEB-02](https://github.com/wispsy/expose_api/issues/87) | Health and search parity pilot | [PNB-WEB-01](https://github.com/wispsy/expose_api/issues/86) |
| [PNB-WEB-03](https://github.com/wispsy/expose_api/issues/88) | Authenticated checked publishing and upload parity | [PNB-WEB-02](https://github.com/wispsy/expose_api/issues/87) |
| [PNB-WEB-04](https://github.com/wispsy/expose_api/issues/89) | Remaining groups, full acceptance and default switch | [PNB-WEB-03](https://github.com/wispsy/expose_api/issues/88) |

The initial double batch is WEB-00 through WEB-03 (four focused PRs), followed
by WEB-04 through WEB-06 (three focused PRs). Start each ready PR's CI, continue
on its local dependent branch, then collect results. Squash passing parents
first, merge current main into immediate children without rewriting published
history, retarget and check focused diffs before merging. Remove parent branches
after all dependents are reconciled. Verify final main.

## Verification and completion gates

Each changed boundary receives direct ASGI or focused standard-library tests.
Cover duplicate headers and cookies, malformed percent/UTF-8/framing, actual
streamed limits, disconnect/cancellation, route collisions/order, mount scopes,
authorization-before-body, lifecycle, and failure after response-start. Run the
relevant full suite before pushing; unchanged optional profiles receive their
existing CI coverage. New modules enter the explicit distribution allowlist.
The existing Linux/macOS/Windows workflow qualifies source and installed wheel
and source artifacts, zero mandatory dependencies and passive imports.

The first double batch completes a usable general HTTP foundation. It does not
complete streaming, multipart, custom-route OpenAPI, static/template adapters or
the Pretty Notebook replacement. Migration acceptance requires the existing web
suite and focused installed/server/container/restart/multi-worker checks with the
selected factory. Broad FastAPI compatibility and performance superiority are
not acceptance claims.

Package publication, release tags and live deployment remain separately
authorized operations. This plan does not change the deployed application.
