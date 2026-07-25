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

- **Secrets must not reach persistent state.** Target keys and tracking
  records are written to CocoIndex's store and are expected to contain only
  logical identifiers. A path that puts a DSN, an API key, or raw document
  content into a tracking record, a target key, or a log line is a bug in this
  category, and there is a test for the log case
  (`test_apply_logs_never_contain_document_content`).
- **Document identity must not be forgeable across tenants or datasets.**
  `data_id` is a `uuid5` over length-prefixed logical coordinates
  (`docs/adr/0002`). An input that makes two distinct
  `(runtime, tenant, dataset, key)` tuples collide would let one dataset
  overwrite another's documents.
- **Untrusted document content must stay data.** Content is fingerprinted and
  handed to Cognee; it never reaches a query, a path, or a shell.
- **The PostgreSQL lock provider takes a DSN.** It is passed straight to
  `asyncpg`; the scope string is hashed, never interpolated.

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
