# CocoIndex audit findings (target-connector surface)

Audited commit: `20a1f4be5dfa9f178aff3463937380006a2fe959` (= release v1.0.18).
Scope: everything a third-party target connector touches. File references are
relative to the cocoindex repo root.

## The connector contract (verified in code, not docs)

- The entire authoring surface is pure Python and re-exported from the
  top-level `cocoindex` package; the implementation lives in
  `python/cocoindex/_internal/target_state.py`. Writing a connector requires
  no Rust, but running one requires the compiled `cocoindex` wheel (the engine
  is Rust/PyO3, module `cocoindex._internal.core`).
- `TargetHandler` is a structural `Protocol` (`target_state.py:197`); the one
  required method:

  ```python
  def reconcile(key, desired_target_state, prev_possible_records, prev_may_be_missing, /) \
      -> TargetReconcileOutput | None
  ```

  It is **synchronous and must not block**: the Rust engine invokes it while
  holding a tokio mutex over the declared-states map
  (`rust/core/src/engine/execution.rs:950-971`). All I/O belongs in sinks.
- `TargetReconcileOutput(action, sink, tracking_record, child_invalidation)`
  (`target_state.py:188`). `None` from reconcile = no-op. The action type is
  connector-defined; `tracking_record=NON_EXISTENCE` records a delete.
- Sinks: `TargetActionSink.from_fn` / `from_async_fn` (`target_state.py:143`),
  callback `(context_provider, actions, /)`. Container sinks must return a
  `ChildTargetDef | None` per action, same length and order (enforced:
  `execution.rs:1642`, `target_state.rs:176`). Sink callables are deduped via
  `WeakValueDictionary`, so equal shared sinks collapse into one batcher —
  prefer module-level or per-instance sinks created once.
- Registration: `register_root_target_states_provider(name, handler)`
  (`target_state.py:305`) wraps the handler so tracking records are
  auto-deserialized based on the type annotation of `reconcile`'s third
  parameter (msgspec-based; `target_state.py:56-92`). Tracking records should
  be `msgspec.Struct(frozen=True)`, dataclasses, NamedTuples, or raw `bytes`.

## Ordering and failure model (`rust/core/src/engine/execution.rs`)

Per component update: **precommit → external sink apply → commit**.

1. Precommit (`pre_commit`, :680) runs reconcile for all declared states
   inside one LMDB transaction and persists the *intended* tracking records as
   additional versioned states next to the previous ones (multi-state), plus a
   pending-process token when sink work was queued. Tracking is durable
   *before* any external write.
2. Sink apply (`submit`, :1628-1680) executes batched actions. On failure the
   pending token is cleared (retried indefinitely) but the multi-state stays.
3. Commit (:1689-1699) collapses multi-state to the confirmed record and
   prunes stale versions.

Next-run semantics after a failure: the item carries multiple
`prev_possible_records`. `prev_may_be_missing=True` is forced when a `Deleted`
marker or pending state is present, on provider schema-version mismatch, on
full reprocess, or when there is no previous item at all (:926-943, :1100-04).
Two live values without a delete marker keep `prev_may_be_missing=False` — the
handler's own record comparison must decide. Pinned by
`python/tests/core/test_component_target_states.py::test_prev_may_be_missing_after_failed_update`
and `::test_proceed_with_failed_creation`.

## Two-level targets and invalidation

- A container declares children by having its sink return
  `ChildTargetDef(handler=child_handler)`; the child handler is constructed in
  the parent sink with live runtime context (resolved connection), never at
  declare time. Reference implementations:
  `python/cocoindex/connectors/sqlite/_target.py` (table→row),
  `localfs/_target.py` (dir→file), `postgres/_target.py`.
- `child_invalidation="destructive"` mints a new provider generation — all
  prior child tracking is ignored (rebuild). `"lossy"` bumps the provider
  schema version — every child sees `prev_may_be_missing=True` (conservative
  replay). Derivation pattern via statediff:
  `sqlite/_target.py:999-1020`. Tests:
  `python/tests/core/test_provider_generation.py`.
- Attachments: optional handler method `attachments() -> dict[str, TargetHandler]`
  (plural). Attachment states live under a symbol-namespaced key and get
  orphan cleanup even when not re-declared (`target_state.rs:347-387`).

## connectorkits utilities worth reusing

- `connectorkits/statediff.py`: `diff()` → `insert/upsert/replace/delete/None`
  from `(desired, prev, prev_may_be_missing)`; `diff_composite()` for
  container+sub-state records; `MutualTrackingRecord` +
  `resolve_system_transition()` implement `managed_by="system"|"user"`
  semantics (user-managed short-circuits cleanup).
- `connectorkits/fingerprint.py`: `fingerprint_bytes/str/object` (memo-key
  based canonical fingerprints).
- `connectorkits/target.py` contains only `ManagedBy` — nothing else.
- Reusable DB-free test harness: `python/tests/common/target_states.py`
  (`DictTargetStateStore`, two-level `DictsTargetStateStore`,
  `AttachmentDictsTargetStateStore`, `Metrics`, `sink_exception` fault flag).
  cogindex's connector tests re-implement this pattern.

## Key and identity rules

- Target keys must be `StableKey`
  (`None | bool | int | str | bytes | UUID | Symbol | tuple[...]`), stable
  across runs. Connection parameters, URLs, credentials, and live objects are
  forbidden in keys; external resources are referenced by a `ContextKey` string
  and resolved at sink time via `context_provider.get(key, T)`
  (`dev/agent-skills/target-connector/SKILL.md:132-158`, AGENTS.md).
- `ContextKey(key_str)` enforces global uniqueness of the string per process.

## Documentation discrepancies found (docs vs code)

1. `dev/agent-skills/target-connector/SKILL.md` and its `attachments.md`
   describe a singular `attachment(self, att_type) -> TargetHandler | None`
   handler method. The engine actually calls **`attachments()` → dict**; the
   singular form exists only on the user-side provider
   (`TargetStateProvider.attachment`). The `.mdx` doc and all shipped
   connectors use the plural form.
2. SKILL.md references docs paths under `docs/docs/...` and `docs/sidebars.ts`
   (Docusaurus); the repo migrated to Astro — real docs live under
   `docs/src/content/docs/**.mdx`, and connector sources are `_target.py`,
   not `target.py`.
3. No dedicated statediff unit-test file exists; statediff is covered
   indirectly through connector tests.

None of these affect runtime behavior; they matter when reading upstream docs.

## Risk assessment for cogindex

- The API sits under "Advanced Topics" with no stability label; it changed
  fundamentally from v0 (decorator/`export()` model) to v1. Mitigation:
  `UPSTREAM_LOCK.json` pin, compat checks, nightly upstream CI job.
- Reconcile runs under an engine lock: any accidental I/O in cogindex's
  reconcile would stall the engine. Structurally prevented rather than tested
  for: `reconcile()` is synchronous and the only object it can reach is its own
  `DatasetHandle`, since the runtime is bound at sink time. Every I/O-capable
  call lives behind `await` in a sink, so accidental blocking I/O in reconcile
  would have to be newly introduced code, not a misuse of an existing seam.
