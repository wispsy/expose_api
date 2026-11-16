# Explicit web application contracts

The EXP-WEB work extends the completed HTTP foundation before the Pretty Notebook
compatibility harness. Core imports and all features here use the standard
library. Application credentials, databases and rendering engines remain
caller-owned.

## Typed request providers

Register providers before serving with `app.provide(Service, provider)`.
Providers receive the request and may return a service, an awaitable service,
or a synchronous/asynchronous context manager yielding the service. Resolve
explicitly with `service = await request.resolve(Service)`. The result must be
an instance of the registered type. No handler signature inspection is involved.

Each request caches both successful and failed provider executions. Concurrent
callers share one execution; dynamic and concurrent dependency cycles fail with
ConfigurationError. Providers may resolve other registered services. Synchronous
providers and context-manager methods run inline and must remain nonblocking.

Resources stay open through exception handlers and streamed response completion.
Cleanup cancels and settles unfinished providers, then exits acquired contexts
in reverse order. Contexts see the application/transport error when present;
they cannot suppress it. Cleanup is reconciled through repeated cancellation.
An error during cleanup after response-start propagates to the server; no second
response is attempted. A retained request cannot resolve services after closing.

`with app.override_providers({Service: replacement}): ...` selects task-local
overrides for registered types. Nested scopes restore the previous selection,
including failures; concurrent contexts do not mutate the application registry.
Request scopes snapshot overrides on first resolution. Overrides do not bypass
route authentication or authorization.

## Authenticated access policies

`@app.route('/admin', access=Authenticated(scopes={'admin'}, tenant=True,
checks=(can_read,)))` selects identity verification and authorization together.
The application must configure its caller-owned authenticator and challenge.
Do not combine `access` with `public`, `authorize` or `authenticate` flags.
Existing explicit authorization callbacks retain their original behavior.

Missing identity returns 401 with the configured challenge. All selected scopes
must be present; `tenant=True` requires tenant information and a string requires
an exact tenant match. Missing permissions return 403 before dependencies or body
reads. Ordered synchronous/asynchronous checks receive the verified request,
must return bool and short circuit on denial. Checks can resolve typed services.
Tenant ownership of individual records still belongs to record policies or
application permission checks; selecting a tenant does not infer record access.

Protected successes and errors force no-store. Router inclusion preserves policy
selection. OpenAPI describes required scopes/tenant and the existing challenge;
metadata cannot select an anonymous alternative for authenticated routes.
Credential verification, session/account storage and token issuance remain
application-owned.

## Development arenas

The canonical queue is [Project 7](https://github.com/orgs/wispsy/projects/7):

| Arena | Contract |
| --- | --- |
| [EXP-WEB-01](https://github.com/wispsy/expose_api/issues/132) | Typed request dependencies and resource cleanup |
| [EXP-WEB-02](https://github.com/wispsy/expose_api/issues/133) | Authenticated access policy composition |
| [EXP-WEB-03](https://github.com/wispsy/expose_api/issues/134) | Public asynchronous ASGI testing client |
| [EXP-WEB-04](https://github.com/wispsy/expose_api/issues/135) | Validated custom handler contracts |
| [EXP-WEB-05](https://github.com/wispsy/expose_api/issues/136) | Opt-in security middleware and 0.2.0 preparation |

These changes do not switch Pretty Notebook factories or publish a package.
Each arena receives local checks and one remote integration PR. Earlier CI is
collected while later work proceeds; the final main CI is deliberately left
running for subsequent review.
