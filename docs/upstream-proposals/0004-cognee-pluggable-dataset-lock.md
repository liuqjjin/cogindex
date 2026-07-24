# Proposal: pluggable cross-process dataset lock provider

**Target:** cognee (audited at `90b4acaa`, applies to released 1.4.0)
**Status:** draft (not yet filed upstream)

## Problem

Cognee's per-dataset write serialization is a **process-local**
`asyncio.Lock` (the code carries an explicit TODO acknowledging it should be
replaced with a distributed mechanism). Two updater processes — or one
library user plus one served API worker — can interleave
add/cognify/forget on the same dataset. The pipeline layers are individually
retry-safe, but concurrent cognify runs over the same items duplicate LLM
work and can interleave graph writes in ways single-process users never see.

## Proposed change

An optional lock-provider protocol resolved from config, defaulting to the
current in-process behavior:

```python
class DatasetLockProvider(Protocol):
    def lock(self, dataset_id: UUID) -> AsyncContextManager[None]: ...
```

with a reference implementation backed by PostgreSQL advisory locks (session
scoped ⇒ crash-safe release for free) for deployments already carrying
Postgres, and the existing asyncio map as the default. The relevant call
sites already funnel through one acquisition point, so the change is
localized.

## What cogindex does today

cogindex wraps its own batch application (deletes → purges → add → cognify)
in a `LockProvider` scope — in-process by default,
`PostgresAdvisoryLockProvider` for multi-process updaters (ADR-0006). That
protects cogindex-driven writes against each other, but cannot protect
against non-cogindex writers inside Cognee itself; only an upstream hook
can close that gap. Correctness of cogindex does not depend on the lock
(idempotent convergent writes), so the exposure is wasted duplicate work,
not corruption.
