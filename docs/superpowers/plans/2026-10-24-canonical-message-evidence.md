# Canonical Message and Evidence Integration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add the v13 strict canonical message layer to current GitHub main and connect its stable Evidence IDs to the existing large-distillation and Nuwa recovery pipeline.

**Architecture:** Parsing and import own source fidelity, identity validation, transactionality and stable identifiers. `echotrace.ingest.canonical` exposes the only downstream source-order view and normalized fingerprint. Existing distillation, disk EvidenceIndex and Nuwa logic retain their recovery behavior while consuming stable string Evidence IDs.

**Tech Stack:** Python 3.11+, FastAPI, SQLite, pytest/pytest-asyncio, existing React/TypeScript UI.

**Spec:** `docs/superpowers/specs/2026-10-24-v13-core-message-layer-design.md`

## Global Constraints

- Preserve current large-distillation recovery, output growth, Nuwa validation, Web/API, QCE and task-management behavior.
- New imports preserve source order and full message text; downstream code must not reconstruct Evidence IDs independently.
- Existing databases migrate without deleting messages; legacy backfill is deterministic but is not represented as recovered original source order.
- Tests use fictional/local files and simulated QCE only; no real provider, QQ login or real conversation data.
- Do not implement dynamic full chunking, `distill_manifest.json` or formal Reply in this branch.

## Review Focus

- An input file changing between validation and commit must roll back every staged database change.
- Reimporting identical files or platform message IDs must preserve existing sequence/Evidence IDs and add no rows.
- Legacy databases with partially applied columns/indexes must migrate repeatedly without renumbering populated rows.
- Long-message fragments and bounded excerpts must retain one stable Evidence ID while carrying offset metadata.
- Existing QCE auto-import must record provenance without making QCE failure affect local-file import.

---

### Task 1: Strict QQ TXT decoding and source-order parsing

**Files:**
- Modify: `echotrace/ingest/parsers/__init__.py`
- Test: `tests/test_core_messages.py`

**Interfaces:**
- Produces: `parse_file(path: Path, custom_pattern: str | None = None, encoding: str | None = None)` yielding normalized rows containing selected `encoding` when applicable.

- [ ] **Step 1: Add failing parser tests**

Add focused tests for BOM UTF-8/UTF-16, strict GBK, invalid bytes, explicit wrong encoding, ambiguous valid candidates, source order, multiline/boundary whitespace, media placeholders and unknown-record line numbers.

- [ ] **Step 2: Run parser tests and verify RED**

Run: `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest -p pytest_asyncio.plugin -q tests/test_core_messages.py -k 'qq_txt or inline_txt or header_txt'`

Expected: failures caused by missing strict decoding/line reporting behavior.

- [ ] **Step 3: Implement strict TXT candidate selection and record parsing**

Decode without replacement, prefer BOM, validate candidates by message headers, require explicit encoding for unresolved ambiguity, preserve source-order body text and attach selected encoding.

- [ ] **Step 4: Run parser tests and verify GREEN**

Run the command from Step 2; expected: selected tests pass.

- [ ] **Step 5: Commit named parser paths**

Commit message: `feat: parse QQ text without losing source fidelity`

### Task 2: Canonical schema and restart-safe migration

**Files:**
- Modify: `echotrace/db/schema.sql`
- Modify: `echotrace/db/migrate.py`
- Test: `tests/test_core_messages.py`

**Interfaces:**
- Produces: message columns `sequence`, `evidence_id`, `source_index`; table `import_sources`; per-session uniqueness indexes.

- [ ] **Step 1: Add failing fresh-schema and legacy-migration tests**

Assert fresh databases expose the new fields, legacy rows receive deterministic continuous values, repeated migration preserves assigned values, and partially migrated schemas recover safely.

- [ ] **Step 2: Run migration tests and verify RED**

Run: `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest -p pytest_asyncio.plugin -q tests/test_core_messages.py -k 'schema or migration or legacy'`

Expected: missing columns/table/indexes or migration behavior fails.

- [ ] **Step 3: Implement schema and idempotent backfill migration**

Backfill per session in existing stored order, never overwrite populated stable fields, create indexes after valid backfill, and record import source metadata without deleting legacy rows.

- [ ] **Step 4: Run migration tests and verify GREEN**

Run the command from Step 2; expected: selected tests pass.

- [ ] **Step 5: Commit named database paths**

Commit message: `feat: persist canonical message identity`

### Task 3: Transactional two-party import and dual fingerprints

**Files:**
- Create: `echotrace/ingest/canonical.py`
- Modify: `echotrace/ingest/cleaner.py`
- Modify: `echotrace/api/routes.py`
- Modify: `echotrace/cli.py`
- Modify: `web/src/ImportDialog.tsx`
- Modify: `web/src/api.ts`
- Test: `tests/test_core_messages.py`
- Test: `tests/test_workflow.py`
- Test: `tests/test_cli.py`

**Interfaces:**
- Produces: `message_fingerprint(rows) -> str` and `canonical_messages(db, contact_id) -> dict`.
- Extends: `import_files(..., encoding=None, source_meta=None)` returning source and normalized fingerprints.

- [ ] **Step 1: Add failing import contract tests**

Cover exact self matching, third-speaker rejection, empty cleaned input, complete long text, source-order sequence/Evidence assignment, identical-source idempotency, platform-ID idempotency, source mutation rollback, normalized fingerprint stability and QCE provenance.

- [ ] **Step 2: Run import contract tests and verify RED**

Run: `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest -p pytest_asyncio.plugin -q tests/test_core_messages.py`

Expected: failures caused by missing canonical module, schema use or transactional import behavior.

- [ ] **Step 3: Implement canonical fingerprint and reader**

Fingerprint only stable normalized fields; validate zero-based sequence and `m000001`-style continuity; include source metadata separately.

- [ ] **Step 4: Implement validation pass and transactional staged import**

Validate identities before writes, assign stable IDs in source order, preserve complete text, verify files before commit and roll back on any failure.

- [ ] **Step 5: Wire explicit encoding and source metadata through API, CLI and import UI**

Keep optional fields backward compatible and do not expose message content in logs.

- [ ] **Step 6: Run focused import and existing workflow tests**

Run: `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest -p pytest_asyncio.plugin -q tests/test_core_messages.py tests/test_cli.py tests/test_workflow.py`

Expected: all selected tests pass.

- [ ] **Step 7: Commit named import/API/UI paths**

Commit message: `feat: import canonical messages transactionally`

### Task 4: Stable Evidence IDs in distillation and disk index

**Files:**
- Modify: `echotrace/distill/pipeline.py`
- Modify: `echotrace/distill/evidence.py`
- Modify: `echotrace/distill/extraction.py`
- Modify: `echotrace/distill/quotes.py`
- Modify: `echotrace/distill/nuwa.py`
- Modify: `echotrace/llm/client.py`
- Modify: `echotrace/rag/indexer.py`
- Test: `tests/test_core_messages.py`
- Test: `tests/test_large_distill.py`
- Test: `tests/test_nuwa.py`
- Test: `tests/test_nuwa_skill_citations.py`
- Test: `tests/test_nuwa_source_recovery.py`
- Test: `tests/test_reported_observations.py`

**Interfaces:**
- Consumes: `canonical_messages(db, contact_id)` and string `evidence_id`.
- Produces: source-order pipeline inputs and an EvidenceIndex whose message primary key accepts stable string IDs.

- [ ] **Step 1: Add failing downstream compatibility tests**

Assert source-order inputs, string Evidence validation, fragment/excerpt identity retention, EvidenceIndex lookup and Nuwa citation allowlists using `mNNNNNN` values.

- [ ] **Step 2: Run compatibility tests and verify RED**

Run: `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest -p pytest_asyncio.plugin -q tests/test_core_messages.py tests/test_large_distill.py tests/test_nuwa_skill_citations.py`

Expected: integer-ID assumptions or timestamp ordering cause failures.

- [ ] **Step 3: Adapt pipeline preparation to the canonical source-order view**

Retain current sampling and chunk/recovery behavior, but make all selected messages carry stable Evidence IDs and never reorder canonical input by timestamp.

- [ ] **Step 4: Adapt EvidenceIndex and validation helpers to string IDs**

Store and query text IDs, preserve fragment offsets, and keep current ownership/speaker/quote validation intact.

- [ ] **Step 5: Run distillation, Nuwa and recovery regression groups**

Run: `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest -p pytest_asyncio.plugin -q tests/test_large_distill.py tests/test_extraction_output_growth.py tests/test_empty_quote_recovery.py tests/test_nuwa.py tests/test_nuwa_skill_citations.py tests/test_nuwa_source_recovery.py tests/test_reported_observations.py tests/test_resume_budget.py`

Expected: all selected tests pass.

- [ ] **Step 6: Commit named downstream paths**

Commit message: `feat: use stable evidence ids across distillation`

### Task 5: Documentation and full verification

**Files:**
- Modify: `README.md`
- Modify: `docs/进度.md`
- Modify: `docs/快速启动提示词.md`
- Modify: `docs/superpowers/plans/2026-10-24-canonical-message-evidence.md`

**Interfaces:**
- Documents verified behavior and remaining Phase 2+ gaps without upgrading unverified claims.

- [x] **Step 1: Update documentation with verified scope and migration limits**

Describe strict decoding, source-order Evidence IDs, fingerprints, legacy migration boundaries and unchanged sampled/fixed chunk behavior.

- [ ] **Step 2: Run the complete Python suite**

Run: `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest -p pytest_asyncio.plugin -q`

Expected: zero failures; report warnings separately.

- [x] **Step 3: Run production build**

Run: `python scripts/build.py`

Expected: Python checks, TypeScript checks and production asset packaging exit 0.

- [x] **Step 4: Check repository diff integrity**

Run: `git diff --check && git status --short`

Expected: no whitespace errors and only intended named paths changed.

- [ ] **Step 5: Commit documentation and verified final state**

Commit message: `docs: record canonical message integration`

- [ ] **Step 6: Push without merging**

Run: `git push -u origin feature/canonical-message-evidence`

Expected: the remote branch tracks `origin/feature/canonical-message-evidence`; `main` remains unchanged.
