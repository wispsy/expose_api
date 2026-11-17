# Pretty Notebook migration readiness

Assessment: 17 November 2026. Framework baseline: Expose API `58e594b`;
application baseline: Pretty Notebook `708a3d0` (prepared 0.12.0).
The framework baseline's six cross-platform checks passed in
[main CI](https://github.com/wispsy/expose_api/actions/runs/43989577155).
Release qualification is tracked by [REL-02](https://github.com/wispsy/expose_api/issues/144).

The HTTP foundation and EXP-WEB contracts are sufficient to begin the staged
application migration. This conclusion qualifies the available framework
interfaces; observable application parity is the PNB-WEB acceptance gate.

| Application requirement | Available interface | Application migration work |
| --- | --- | --- |
| Factory and startup/shutdown | Native ASGI App and caller-owned lifespan | Initialize Tortoise directly, migrate under the existing process lock and retain shared worker state |
| Health, search and query validation | Explicit routes, repeated query fields, bounded requests, JSON responses | Preserve Pydantic validation and existing 422/error ordering explicitly |
| Token authentication and permissions | Request hooks, typed providers, authenticated policies | Preserve current JWT, bootstrap and notebook scope checks before body reads |
| Checked publishing | Explicit headers/status/JSON, dependencies, bounded bodies | Reuse revisions, ETags, idempotency, transaction and audit services |
| Image/form uploads | Bounded native multipart and owned upload cleanup | Preserve accepted media, image hashing, field names, limits and failures |
| Pages, forms, redirects and cookies | HTML/redirect/cookie responses and caller-owned template adapter | Keep Jinja2 and existing templates; port view handlers explicitly |
| Static assets and conditional files | Rooted static mounts, files, HEAD, ranges and conditional requests | Retain image-before-static precedence, cache rules and root prefixes |
| Host/security/audit middleware | Ordinary ASGI middleware and selected security profiles | Match current CSP, headers, API no-store and operation logs |
| Protocol test harness | Public asynchronous ASGI client and lifespan control | Reuse baseline fixtures, compare both factories and run real-server acceptance |
| Persistence and validation | Optional published Backpack and Enforce Type adapters | Keep Tortoise/Pydantic application-owned initially; no automatic ORM translation |

The application inventory contains 41 decorated route handlers plus the main
health and unknown-API handlers, two static mounts and FastAPI's generated
OpenAPI/documentation routes. The migration must register explicit handlers;
there is no FastAPI signature or response-model emulation. In particular,
`/static/favicon/{version}.png` needs a whole-segment parameter with application
validation of the suffix, and dynamic assets must retain their precedence over
the general static mount. Public URLs can remain unchanged through these adapters.

The current web factory explicitly accepts SQLite only. Its migrations use
SQLite PRAGMAs, file snapshots and a host-level process lock. PostgreSQL is
deferred entirely to a future session and is not a framework-migration gate.
Backpack 0.2.0 supplies the existing SQLite snapshot/application profile;
Enforce Type 0.2.0 supplies optional compiled validation. Neither requires an
application-wide storage or model rewrite for this migration.

Before publication, the release branch replaces the development Git requirement
with `backpack-store[enforce-type]==0.2.0`, selects `enforce-type==0.2.0`, verifies
MIT license files/metadata and qualifies installed wheel/source profiles.
The core remains dependency-free on Python 3.14+.

Publish the verified library releases and Pretty Notebook's existing 0.12.0
baseline, then implement [PNB-WEB-01](https://github.com/wispsy/expose_api/issues/86)
through [PNB-WEB-04](https://github.com/wispsy/expose_api/issues/89): selectable
factory/harness, health/search pilot, authenticated publishing/uploads, and
remaining groups/default switch. Keep FastAPI as the baseline until parity
passes. Final acceptance includes installed profiles, real HTTP, restart and
worker tests. Application deployment remains separately authorized.
