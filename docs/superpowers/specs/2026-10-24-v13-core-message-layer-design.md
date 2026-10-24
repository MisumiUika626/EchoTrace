# EchoTrace v13 Core Message Layer Integration Design

## Intent

Use the current GitHub `main` implementation as the base, preserving its large-distillation recovery, disk-backed evidence index, Nuwa validation, Web/API, QCE and task-management capabilities. Integrate the stricter v13 core message guarantees already implemented in the earlier local checkout, then connect the existing distillation pipeline and EvidenceIndex to the stable canonical message view.

This branch is the first independently testable migration stage. It does not claim that the full v13 course loop is complete.

## Goals

- Strict QQ TXT decoding with BOM priority, strict UTF-8/GBK/UTF-16 candidates, header validation, explicit encoding override and line-addressable failures.
- Preserve source order, multiline bodies, boundary newlines, media placeholders and complete message text in permanent storage.
- Require an exact self identity and exactly one other non-system identity after cleaning; reject empty cleaned imports and third-speaker conflicts.
- Make import writes transactional and idempotent, with file-change detection during import.
- Persist stable source-order `sequence`, continuous `evidence_id` values beginning at `m000001`, and `source_index`.
- Persist source SHA256, normalized-message SHA256, detected encoding and source provenance for local-file and QCE imports.
- Expose one canonical message reader that validates continuity and computes the normalized fingerprint.
- Adapt existing distillation input preparation and disk-backed EvidenceIndex to use source order and stable string Evidence IDs without removing the current recovery/checkpoint behavior.
- Preserve existing JSON, JSONL, HTML and XLSX import behavior with regression coverage.

## Non-goals

- Dynamic Whole/Chunked maximum-fill planning, tokenizer/model capability metadata or formal full-mode coverage proof.
- `distill_manifest.json`, its `complete` gate or stale-Skill reply rejection.
- The v13 three-style formal Reply contract or its CLI.
- Real-model quality validation, real QCE login/export or proof of Tencent-side completeness.
- Removing sampled quick/standard modes or replacing the current Nuwa workflow.

## Constraints

- Existing large-distillation recovery, output-growth, EvidenceIndex, Nuwa citation recovery and task cancellation behavior must remain.
- Existing user data must migrate without deleting messages. Legacy rows receive deterministic sequence/Evidence values in their existing stored order; the migration must not claim those values reconstruct unavailable original source order.
- New imports use source order. Distillation must no longer reorder canonical input by timestamp.
- Permanent message text is not truncated. Display/model excerpts remain bounded views and must retain stable source identity.
- Tests use fictional/local data and simulated QCE only. No real provider or QQ interaction.
- Schema migration must be restart-safe and compatible with existing databases.

## Approaches considered

### Selected: Forward-port into current GitHub main

Keep the newer GitHub architecture and reimplement the stricter message layer against it. Adapt downstream code to stable Evidence IDs through explicit boundaries. This retains newer reliability work and gives each compatibility change a focused regression test.

### Rejected: Replace current main with the earlier local checkout

This would recover the strict import layer quickly but discard the newer extraction split tree, output-budget recovery, EvidenceIndex, Nuwa citation recovery and fixed worker pool.

### Rejected: Mechanical file copying

The two trees changed the same schema, cleaner, parser and pipeline files for different reasons. Whole-file replacement would silently regress one side and would not demonstrate compatibility.

## Architecture

### Strict parsing boundary

`echotrace.ingest.parsers.parse_file()` accepts an optional explicit encoding. TXT parsing reads bytes, selects a strict candidate using BOM and valid-message-header evidence, and returns the selected encoding with normalized rows. Unknown preamble/body records fail with a line number instead of being discarded.

### Transactional import boundary

`echotrace.ingest.cleaner.import_files()` performs a validation pass before writes. It records source fingerprints, validates the two-party identity contract, stages all new messages in the database transaction, verifies source files did not change, and only then publishes contacts, sessions, messages and import-source metadata.

Idempotency prefers a platform message ID when available; otherwise it uses source-file fingerprint plus source position. Existing message hashes remain readable for compatibility.

### Canonical message boundary

`echotrace.ingest.canonical` owns the stable fingerprint fields and returns messages ordered by `sequence`. It rejects discontinuous sequences or Evidence IDs. Downstream distillation uses this view, never reconstructs evidence identifiers independently, and retains database IDs only as internal lookup metadata.

### Storage and migration

`messages` gains `sequence`, `evidence_id` and `source_index`, with per-session uniqueness indexes. `import_sources` records file/source fingerprints, normalized fingerprint, encoding, import time and provenance. Migration backfills legacy rows deterministically and marks that provenance as legacy-derived where applicable.

### Distillation and EvidenceIndex compatibility

Pipeline input preparation reads canonical messages in source order and keeps stable string Evidence IDs in prompts, checkpoints and validations. The disk index stores string IDs rather than requiring SQLite integer message IDs. Existing excerpts and long-message fragments keep the same Evidence ID plus their offset metadata.

The current sampled tiers and fixed chunk sizing remain explicitly unchanged in this branch; replacing them belongs to the later dynamic-capacity phase.

## Verification

- New focused tests first demonstrate failures for encoding, ordering, multiline preservation, identity conflicts, empty imports, full-text retention, stable IDs, dual fingerprints, idempotency, source mutation and QCE provenance.
- Migration tests cover legacy databases and restart safety.
- Existing import, QCE, workflow, large-distillation, EvidenceIndex, Nuwa and recovery tests must remain green.
- Run the full Python suite, `git diff --check`, and the production build before committing the completed integration.

## Delivery

- Work on branch `feature/canonical-message-evidence`.
- Commit only named source, tests and documentation paths; do not commit databases, real chats, secrets, logs or real model responses.
- Push the branch to the user-provided GitHub repository after local verification. Do not merge it into `main`.
