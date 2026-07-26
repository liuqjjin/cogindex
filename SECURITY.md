# Security

## Reporting a vulnerability

Report privately through
[GitHub's advisory form](https://github.com/liuqjjin/cogindex/security/advisories/new)
rather than opening an issue. Expect an acknowledgement within a few days and
a fix or an explanation of why it is not one, before any public disclosure.

Please include what an attacker gains, not only what misbehaves: this is a
library that runs inside someone else's pipeline, so the interesting reports
are the ones that cross a trust boundary.

## What is in scope

cogindex holds no credentials of its own and opens no listening sockets. Its
security-relevant surface is narrow, and these are the properties worth
attacking:

- **Connector data must not copy secrets into persistent state.** Target keys
  and tracking records are written to CocoIndex's store. Runtime names are
  restricted to a 1–128 character logical-identifier grammar, which rejects
  URL and DSN forms; logs and records do not copy runtime objects, upstream
  error payloads or document content. Tests cover those paths. A caller-chosen
  opaque API key made only of allowed characters cannot be distinguished from
  a logical name, so the `ContextKey` itself must still be non-secret.
- **Document identity must not be forgeable across tenants or datasets.**
  `data_id` is a `uuid5` over length-prefixed logical coordinates
  (`docs/adr/0002`). An input that makes two distinct
  `(runtime, tenant, dataset, key)` tuples collide would let one dataset
  overwrite another's documents.
- **Untrusted document content must stay data.** Content is fingerprinted and
  handed to Cognee; it never reaches a query, a path, or a shell.
- **The PostgreSQL lock provider takes a DSN.** It is passed straight to
  `asyncpg`; the scope string is hashed, never interpolated.

## Known dependency advisory

Cognee 1.4.0 transitively installs `diskcache` 5.6.3, which is affected by
[PYSEC-2026-2447 / GHSA-w8v5-vhqr-4h9v](https://github.com/advisories/GHSA-w8v5-vhqr-4h9v).
Exploitation requires an attacker who can write to the cache directory. Keep
Cognee's cache and storage directories writable only by the service account.

No fixed `diskcache` release is currently available. CI ignores only
`PYSEC-2026-2447`; every other `pip-audit` finding remains blocking. Remove
that exemption as soon as Cognee or `diskcache` provides an upgrade path.

## What is out of scope here

- Vulnerabilities in [cocoindex](https://github.com/cocoindex-io/cocoindex) or
  [cognee](https://github.com/topoteretes/cognee) themselves. Report those
  upstream. If cogindex's usage makes an upstream issue reachable when it
  otherwise would not be, that part is in scope here.
- The consequences of pointing a `LocalCogneeRuntime` at storage roots or a
  database you do not control.
- LLM prompt injection through document content. Cognee owns the extraction
  prompt and the model call; cogindex decides only which documents are sent.

## Supported versions

Pre-1.0. Fixes land on `main` and in the next release; there are no backports
to earlier 0.x versions.
