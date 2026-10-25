# Production Full/Chunked Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make new `full` distillation jobs execute deterministic Whole/Chunked plans that completely cover authorized canonical messages while preserving the legacy sampled workflow.

**Architecture:** Extract Phase 2 packing into a pure shared planner that admits both observation and quote request envelopes, adds bounded overlap, and returns immutable coverage metadata. Dry-run sanitizes that plan, while production persists the exact views, validates first coverage against canonical storage, and then reuses the current worker/extraction/Nuwa pipeline; API, CLI, and Web select full or explicitly incomplete sampled mode.

**Tech Stack:** Python 3.11, FastAPI, Pydantic, SQLite, React/TypeScript, pytest, existing deterministic JSON/checkpoint helpers.

**Spec:** `docs/superpowers/specs/2026-10-25-production-full-chunking-design.md`

## Global Constraints

- Implement only v13 Phase 3; do not implement Phase 4 Map/Reduce/Evidence semantics, Phase 5 `distill_manifest.json`/complete publication, or Phase 6 replies.
- New API/Web/CLI starts default to `full`; persisted jobs without `mode` resolve to legacy `sampled`.
- `full` requires raw authorized bodies and rejects summary-only/privacy-disabled raw text before writing input chunks.
- Full planning and dry-run use the same request envelopes and boundaries; raw chunks must fit both `distill_chunk` and `extract_quotes`.
- Overlap is at most three complete messages and at most 5% of receiving available content; shrink oldest-first to zero when needed for progress.
- Every authorized message or fragment receives first coverage exactly once; every emitted chunk advances by at least one unseen unit.
- Long-message fragments are input views only; canonical database text is never changed and reconstruction must be exact.
- Preserve current sampled quick/standard/deep checkpoints, caches, fixed worker pool, extraction recovery, Nuwa, RAG, and published versions.
- Use only fictional fixtures and mock providers; do not connect to a real model or QCE.
- Stage only named files; never commit generated `data/`, input chunks, databases, secrets, logs, build dependencies, or real chat.

## Review Focus

- One stage fits but the other does not: the tighter of observation and quote requests must control the boundary — Task 1 test.
- Overlap consumes capacity until no new unit fits: oldest overlap must shrink, eventually to zero, without an empty chunk — Task 1 test.
- Repeated fragment IDs and overlap copies reach EvidenceIndex: canonical full text must be reconstructed once, not truncated by `INSERT OR IGNORE` — Task 2 test.
- A pre-upgrade queued payload has no `mode`: it must resume sampled rather than silently becoming an expensive full run — Task 3 test.
- A sampled preview attempts to replace a future `complete: true` active version: publication/start must reject it without changing the active version — Task 3 test.

---

### Task 1: Shared dual-stage planner with bounded overlap

**Files:**
- Create: `echotrace/distill/planner.py`
- Modify: `echotrace/distill/dry_run.py`
- Modify: `echotrace/llm/client.py`
- Test: `tests/test_full_chunking.py`
- Test: `tests/test_distill_dry_run.py`
- Test: `tests/test_request_budget.py`

**Interfaces:**
- Produces: `build_request(llm, stage, settings, context) -> dict`, the exact initial request envelope shared with live calls; planner supplies stage-specific context and `LLMClient.call()` supplies its existing context.
- Produces: frozen `PlannedChunk(views, new_indexes, budgets)` and `FullPlan(mode, chunks, fragmented, source_fingerprint, planner_version)` records.
- Produces: `plan_full_chunks(llm, canonical_rows, settings, target_name, *, overlap_messages=3, overlap_ratio=.05) -> FullPlan`.
- Preserves: `dry_run_distill(...) -> dict`, now sanitizing `FullPlan` and exposing per-stage estimates without chat text.
- Consumed by: Task 2 persists and validates `FullPlan`; Task 3 API dry-run passes the chosen mode intent.

- [ ] **Step 1: Write failing dual-stage and deterministic-boundary tests**

Add tests proving: the same rows/settings/name produce identical serialized plans; changing either stage Prompt/schema/output reserve changes the boundary; a chunk accepted by `distill_chunk` but rejected by `extract_quotes` is split; Whole has one chunk and no overlap.

- [ ] **Step 2: Write failing maximum-fill and overlap tests**

Assert every non-final chunk cannot accept the next unseen unit, overlap is at most three complete messages and within 5%, overlap indexes are excluded from first coverage, oldest overlap shrinks first, zero overlap is used when required, and every chunk has at least one `new_index`.

- [ ] **Step 3: Write failing fragment tests**

Assert fragments are non-empty and continuous, retain one Evidence ID/sequence, have zero-based index/count/offset, reconstruct exactly, never overlap, and each fragment index appears in exactly one chunk's first coverage.

- [ ] **Step 4: Run Task 1 tests and verify RED**

Run: `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest -p pytest_asyncio.plugin -q tests/test_full_chunking.py tests/test_distill_dry_run.py tests/test_request_budget.py`

Expected: planner module/interfaces and dual-stage/overlap behavior are absent.

- [ ] **Step 5: Extract the exact stage request builder**

Implement `build_request(...)` and make `LLMClient.call()` use it before allowance calculation. Preserve recovery identity, output growth, repair messages, adapter parameters, and provider payload semantics; repair attempts may replace `request["messages"]` after the shared initial envelope is built.

- [ ] **Step 6: Implement immutable full planning**

Implement binary-search maximum fill against both stage budgets, overlap selection/shrink, and character-offset fragmentation. Treat only `REQUEST_OVERHEAD_TOO_LARGE` as a fit miss; propagate capacity/configuration errors.

- [ ] **Step 7: Make dry-run sanitize the shared plan**

Return counts, sequence ranges, overlap, fragment counts, capacity/estimator metadata, and both stage token estimates only. Assert recursively that no `text`, display name, secret, or request body is returned.

- [ ] **Step 8: Run Task 1 tests and verify GREEN**

Run: `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest -p pytest_asyncio.plugin -q tests/test_full_chunking.py tests/test_distill_dry_run.py tests/test_request_budget.py tests/test_extraction_output_growth.py`

Expected: zero failures.

- [ ] **Step 9: Commit named Task 1 paths**

Commit message: `feat: share deterministic full chunk planning`

### Task 2: Production full persistence and exact coverage validation

**Files:**
- Modify: `echotrace/distill/pipeline.py`
- Modify: `echotrace/distill/evidence.py`
- Modify: `echotrace/distill/planner.py`
- Test: `tests/test_full_chunking.py`
- Test: `tests/test_core_messages.py`
- Test: `tests/test_large_distill.py`
- Test: `tests/test_workflow.py`

**Interfaces:**
- Consumes: Task 1 `FullPlan` and `plan_full_chunks(...)`.
- Produces: `Pipeline.prepare_full(cid, target, payload, stats, settings, target_name, check) -> dict`.
- Produces: `validate_full_coverage(db, contact_id, target, manifest) -> list[dict]`, returning authorized complete canonical message payloads in sequence order.
- Extends: `EvidenceIndex(target, canonical_messages=None)` so full runs index complete canonical payloads while legacy callers retain their current behavior.
- Persists: `manifest.json` planner identity, per-chunk body-free descriptors, source fingerprint, stage budgets, first-coverage totals, and legacy `chunks/total/sampled/self_examples/source_*` fields.

- [ ] **Step 1: Write failing production/dry-run parity tests**

Create fictional Whole and Chunked jobs and assert production `inputs/*.json` descriptors exactly match dry-run for the same model, Prompt, scope, raw setting, and target name. Assert persisted input recalculation matches recorded budgets for both stages.

- [ ] **Step 2: Write failing complete-coverage validator tests**

Cover strict sequence, scoped source fingerprint, exactly-once first message/fragment coverage, duplicate/gap/reorder rejection, offset/count mismatch, text mutation, overlap exclusion, and exact long-message reconstruction. Expect `INVALID_FULL_COVERAGE` before EvidenceIndex/aggregate stages.

- [ ] **Step 3: Write failing EvidenceIndex fragment/overlap test**

Persist a full plan containing repeated overlap IDs and multiple fragments of one ID; assert the index contains one row per canonical Evidence ID and the long row contains the complete permanent text.

- [ ] **Step 4: Run Task 2 tests and verify RED**

Run: `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest -p pytest_asyncio.plugin -q tests/test_full_chunking.py tests/test_core_messages.py tests/test_large_distill.py -k 'full or coverage or fragment or worker_pool'`

Expected: production still uses the legacy fixed planner and index truncates repeated fragment identities.

- [ ] **Step 5: Implement `prepare_full` and manifest persistence**

Write planned views atomically to numbered input files, keep descriptors body-free, include checkpoint/planner identity, and never mutate canonical rows. On resume, verify source revision/fingerprint and planner identity before reusing inputs.

- [ ] **Step 6: Validate coverage before model work and indexing**

Run `validate_full_coverage(...)` immediately after preparation/resume and again before `EvidenceIndex.build()`. Pass its canonical payloads to EvidenceIndex; map any drift to `INVALID_FULL_COVERAGE` without publishing partial output.

- [ ] **Step 7: Verify live request budgets before provider calls**

For full chunks, rebuild both stage envelopes from each persisted input and compare request/output/safety/capacity/estimator fields with the manifest. Raise `PLANNER_REQUEST_MISMATCH` before transport when different.

- [ ] **Step 8: Preserve workers, recovery, quotes, Nuwa, and sampled preparation**

Route only explicit full mode through `prepare_full`; leave the sampled `prepare` algorithm/checkpoint shape intact. Confirm overlap duplicate observations/quotes remain deduplicated and worker count stays bounded by configured concurrency.

- [ ] **Step 9: Run Task 2 tests and verify GREEN**

Run: `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest -p pytest_asyncio.plugin -q tests/test_full_chunking.py tests/test_core_messages.py tests/test_large_distill.py tests/test_workflow.py tests/test_empty_quote_recovery.py tests/test_reported_observations.py`

Expected: zero failures.

- [ ] **Step 10: Commit named Task 2 paths**

Commit message: `feat: execute complete full distillation inputs`

### Task 3: Mode compatibility, API, and CLI

**Files:**
- Modify: `echotrace/api/routes.py`
- Modify: `echotrace/cli.py`
- Modify: `echotrace/distill/pipeline.py`
- Modify: `echotrace/distill/persona.py`
- Test: `tests/test_full_chunking.py`
- Test: `tests/test_cli.py`
- Test: `tests/test_boundaries.py`
- Test: `tests/test_resume_budget.py`

**Interfaces:**
- Produces: `DistillStart.mode` and `DistillDryRun.mode`, each `full|sampled`; start defaults full, dry-run mirrors the selected intent.
- Produces: `echotrace distill --mode full|sampled`, default full; `--tier` remains accepted and applies only to sampled.
- Produces: `resolve_distill_mode(payload) -> str`, where a missing field means sampled only for persisted legacy job payloads.
- Enforces: full/raw gate and future active-complete protection for sampled starts/publication.

- [ ] **Step 1: Write failing new-default and legacy-resume tests**

Assert new API/CLI requests serialize `mode=full`; explicit sampled retains tier; direct execution of a persisted payload without mode uses sampled; resuming such a payload preserves its existing input/checkpoint hashes.

- [ ] **Step 2: Write failing safety-boundary tests**

Assert full with `send_raw=false`, global summary-only, or `allow_raw=false` raises `FULL_REQUIRES_RAW_TEXT` before input/version writes or provider calls. Assert sampled summary remains valid and visibly incomplete.

- [ ] **Step 3: Write failing future-complete protection test**

Seed an active persona metadata file with `complete: true`; assert sampled start/publication is rejected and active database/file pointers remain unchanged. Full is allowed to create a new candidate but remains `complete_eligible=false` in Phase 3.

- [ ] **Step 4: Run Task 3 tests and verify RED**

Run: `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest -p pytest_asyncio.plugin -q tests/test_full_chunking.py tests/test_cli.py tests/test_boundaries.py tests/test_resume_budget.py -k 'mode or sampled or full or legacy or complete'`

Expected: payload models/CLI flag and compatibility resolver do not exist.

- [ ] **Step 5: Implement typed mode entry points and resolver**

Add the Pydantic fields, CLI flag, request serialization, legacy missing-mode resolution, and stable error envelopes. Dry-run never requires consent; real full/sampled calls retain consent and provider configuration gates.

- [ ] **Step 6: Persist explicit incomplete/full metadata**

Write `distill_mode`, `coverage_mode`, `coverage_ratio`, `first_covered_messages`, `first_covered_fragments`, `fragmented_messages`, and `complete_eligible: false`. Keep legacy `sampled` integer for existing consumers while adding unambiguous labels.

- [ ] **Step 7: Add future complete-version guard**

Read active metadata before starting/publishing sampled; reject only when it explicitly has `complete: true`. Do not infer completeness from old versions or quality flags.

- [ ] **Step 8: Run Task 3 tests and verify GREEN**

Run: `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest -p pytest_asyncio.plugin -q tests/test_full_chunking.py tests/test_cli.py tests/test_boundaries.py tests/test_resume_budget.py tests/test_output_limit.py`

Expected: zero failures.

- [ ] **Step 9: Commit named Task 3 paths**

Commit message: `feat: distinguish full and sampled distillation`

### Task 4: Web mode selection and version labeling

**Files:**
- Modify: `web/src/DistillDialog.tsx`
- Modify: `web/src/Person.tsx`
- Modify: `web/src/zh-CN.ts`
- Modify: `web/src/style.css` only if existing segmented/tier styles are insufficient
- Modify: `echotrace/api/routes.py`
- Test: `tests/test_full_chunking.py`
- Test: `tests/test_workflow.py`

**Interfaces:**
- Consumes: Task 3 API mode and version metadata.
- Produces: default full selector, sampled-preview selector, sampled-only tier controls, matching dry-run/start payloads, and full/Whole/full/Chunked/sampled-preview labels.

- [ ] **Step 1: Write failing API/view-data assertions**

Assert contact/version API responses expose the persisted mode and coverage label needed by Web, and sampled metadata never uses a complete/full label.

- [ ] **Step 2: Implement Web selector and warnings**

Default to full; hide tier controls in full; force raw toggle on and explain why; sampled shows quick/standard/deep plus a prominent incomplete-preview warning. Submit identical mode/scope/model/thinking/raw fields to dry-run and start.

- [ ] **Step 3: Render version/result labels**

Use metadata to display “完整 · Whole”, “完整 · Chunked”, or “抽样预览 · quick|standard|deep”. Treat old missing-mode versions as legacy sampled without rewriting them.

- [ ] **Step 4: Run frontend and workflow verification**

Run: `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest -p pytest_asyncio.plugin -q tests/test_full_chunking.py tests/test_workflow.py`

Run: `PATH=/usr/local/bin:/usr/bin:/bin python scripts/build.py`

Expected: tests and production build exit 0.

- [ ] **Step 5: Commit named Task 4 paths**

Commit message: `feat: label complete and preview distillation modes`

### Task 5: Documentation and Phase 3 verification

**Files:**
- Modify: `README.md`
- Modify: `docs/进度.md`
- Modify: `docs/快速启动提示词.md`
- Modify: `docs/superpowers/plans/2026-10-25-production-full-chunking.md`

**Interfaces:**
- Documents: full versus sampled behavior, overlap rule, privacy/cost implications, CLI/Web usage, legacy compatibility, and unchanged Phase 4+ gaps.

- [ ] **Step 1: Document user-visible mode semantics**

Explain that full covers every authorized raw message but Phase 3 still cannot claim a complete canonical Skill; sampled is cheaper/incomplete and cannot replace future complete output. Include CLI examples for both modes.

- [ ] **Step 2: Run Phase 1–3 targeted regressions**

Run: `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest -p pytest_asyncio.plugin -q tests/test_core_messages.py tests/test_request_budget.py tests/test_distill_dry_run.py tests/test_full_chunking.py tests/test_large_distill.py tests/test_cli.py tests/test_boundaries.py tests/test_workflow.py tests/test_extraction_output_growth.py tests/test_empty_quote_recovery.py tests/test_nuwa_source_recovery.py tests/test_reported_observations.py tests/test_resume_budget.py tests/test_output_limit.py`

Expected: zero failures; report warnings separately.

- [ ] **Step 3: Run the complete Python suite**

Run: `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest -p pytest_asyncio.plugin -q`

Expected: zero failures. Preserve timeout/failure evidence and report honestly if the environment does not complete.

- [ ] **Step 4: Run production build and repository checks**

Run: `PATH=/usr/local/bin:/usr/bin:/bin python scripts/build.py`

Run: `git diff --check && git status --short`

Expected: build exits 0; no whitespace errors; only intended documentation paths remain before the final commit.

- [ ] **Step 5: Commit documentation and final verified state**

Commit message: `docs: record production full chunking`

- [ ] **Step 6: Push the existing feature branch without merging**

Run: `git push origin feature/canonical-message-evidence`

Expected: remote branch advances to the verified HEAD; `main` remains unchanged.
