# Canonical Nuwa Skill Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make a verified full distillation publish a self-contained canonical Nuwa Skill and formal `distill_manifest.json`, with `complete: true` only when every v13 Phase 5 gate passes.

**Architecture:** Extend the existing `NuwaWorkflow` and Skill writer rather than creating a parallel pipeline. A focused canonical validator produces structured gate results, a manifest builder consolidates existing planner/provider/Nuwa facts without guessing, and the pipeline makes one final local gate decision immediately before publication. Sampled remains the legacy-compatible preview route.

**Tech Stack:** Python 3.11+, FastAPI, SQLite, pytest/pytest-asyncio, existing EchoTrace LLM mock transport and atomic JSON utilities.

**Spec:** `docs/superpowers/specs/2026-10-25-canonical-nuwa-skill-design.md`

## Global Constraints

- Implement only AGENTS v13 Phase 5; do not add Phase 6 personalized reply behavior.
- Reuse Phase 3 dynamic Whole/Chunked planning and Phase 4 Map-Reduce/Evidence outputs.
- Preserve existing Nuwa research, blind testing, refinement, Web/API, SQLite, RAG, versioning, rollback, export, and sampled preview behavior.
- `complete: true` requires full coverage, current fingerprints, required canonical files, valid authorized Evidence, passing format and Nuwa semantic quality, and derived-artifact consistency.
- Mechanical validation does not claim semantic Evidence support; real-model and human acceptance remain Phase 8.
- Tests use fictional data and mock providers only; no real QCE, API key, network call, or chat upload.
- Each task follows RED→GREEN and commits named paths only.

## File Structure

- Create `echotrace/distill/canonical_skill.py`: canonical file/Evidence/quality/fingerprint gates and formal manifest construction.
- Modify `echotrace/distill/skill_writer.py`: guarantee portable Evidence summaries and canonical research/quotes output remain self-contained.
- Modify `echotrace/distill/nuwa.py`: expose Nuwa provenance and validation facts needed by the formal gate without changing role isolation.
- Modify `echotrace/distill/pipeline.py`: run the final gate, write `distill_manifest.json`, control `metadata.complete`, and preserve incomplete diagnostics.
- Create `tests/test_canonical_skill.py`: focused pure/integration tests for canonical gates and manifest fields.
- Modify `tests/test_full_chunking.py`: full pipeline complete/incomplete publication tests.
- Modify `tests/test_nuwa.py`: canonical tree, portable Evidence, and Nuwa hash/quality integration tests.
- Modify `docs/进度.md`: record Phase 5 evidence after final verification.

## Review Focus

- Source messages change after Nuwa generation but before publication: completion must fail on a fresh fingerprint check.
- A Markdown citation exists in the database but not in the authorized scope or portable Evidence packet: completion must fail.
- Nuwa reports `passed` but `SKILL.md` bytes changed afterward: completion must fail on the stored hash mismatch.
- A sampled preview has all canonical-looking files: it must still remain incomplete and must not replace an active complete version.
- Resume encounters legacy internal manifests or checkpoints with missing new fields: it may remain readable/incomplete but must never invent values or become complete.

---

### Task 1: Canonical Artifact and Evidence Validator

**Files:**
- Create: `echotrace/distill/canonical_skill.py`
- Create: `tests/test_canonical_skill.py`

**Interfaces:**
- Consumes: `target: Path`, authorized messages from `validate_full_coverage`, `quality: dict`, and `nuwa_report: dict`.
- Produces: `validate_canonical_skill(target: Path, authorized_messages: list[dict], quality: dict, nuwa_report: dict) -> dict` with `gates`, `complete`, and non-sensitive `failures`.
- Produces: `REQUIRED_CANONICAL_FILES: tuple[str, ...]` containing `skill/SKILL.md`, `skill/references/quotes.md`, and all six research paths.

- [ ] **Step 1: Write failing tests for required, non-empty canonical files**

  Add `test_missing_or_empty_required_file_fails_canonical_gate`. Build a literal temporary canonical tree, remove one file and empty another in parametrized cases, and assert `complete is False`, `gates["canonical_files"] is False`, and the relative path appears in `failures`. The production mutation caught is treating path existence alone as sufficient.

- [ ] **Step 2: Run the focused test and verify RED**

  Run: `.venv/bin/pytest -q tests/test_canonical_skill.py::test_missing_or_empty_required_file_fails_canonical_gate`

  Expected: FAIL because `canonical_skill` or `validate_canonical_skill` does not exist.

- [ ] **Step 3: Implement the file gate**

  Implement `REQUIRED_CANONICAL_FILES` and `validate_canonical_skill(...)` with a structured result. Check regular non-empty files; do not parse prose content for exact wording.

- [ ] **Step 4: Write and verify RED tests for Evidence scope and portability**

  Add `test_canonical_citations_must_resolve_in_scope_and_portable_packet`. Use literal `m000001`/`m000002` messages, Markdown citing both, and `references/evidence.json` containing only one. Assert failure until both the authorized set and portable packet contain every cited ID. The production mutation caught is accepting a citation from either source instead of their intersection.

  Run: `.venv/bin/pytest -q tests/test_canonical_skill.py::test_canonical_citations_must_resolve_in_scope_and_portable_packet`

  Expected: FAIL at the Evidence gate.

- [ ] **Step 5: Implement canonical citation validation**

  Reuse `skill_writer.citation_ids` to collect citations from `SKILL.md`, `quotes.md`, and six research documents. Load `references/evidence.json`, require each cited ID in both its message IDs and the authorized message IDs, and report only IDs/paths, never chat text.

- [ ] **Step 6: Write and verify RED tests for Nuwa quality and Skill hash**

  Add `test_nuwa_pass_and_current_skill_hash_are_both_required`. Cover format failure, semantic failure, degraded/stale Nuwa status, and mismatched `validation.skill_sha256`; assert each keeps `complete` false. The production mutation caught is trusting `report.status` or `quality.passed` alone.

  Run: `.venv/bin/pytest -q tests/test_canonical_skill.py::test_nuwa_pass_and_current_skill_hash_are_both_required`

  Expected: FAIL because the Nuwa gate is not implemented.

- [ ] **Step 7: Implement Nuwa and derived-artifact gates**

  Require `quality.passed`, `quality.semantic_passed`, `quality.nuwa_status == "passed"`, `nuwa_report.status == "passed"`, and a current SHA256 match. Require the existing derived files `persona.json`, `radar.json`, `quotes.jsonl`, `metrics.json`, `self_style.json`, and `skill_quality.json`; recursively reject their explicit `evidence` IDs when outside the authorized set.

- [ ] **Step 8: Run Task 1 tests and commit**

  Run: `.venv/bin/pytest -q tests/test_canonical_skill.py`

  Expected: PASS.

  Commit:

  ```bash
  git add echotrace/distill/canonical_skill.py tests/test_canonical_skill.py
  git commit -m "feat: validate canonical nuwa skill"
  ```

### Task 2: Self-contained Portable Skill Output

**Files:**
- Modify: `echotrace/distill/skill_writer.py`
- Modify: `echotrace/distill/nuwa.py`
- Modify: `tests/test_nuwa.py`

**Interfaces:**
- Consumes: the current-version Evidence packet selected by `NuwaWorkflow` and canonical Skill structures.
- Produces: `skill/references/evidence.json` whose messages cover all citations emitted into canonical Markdown and contain enough stable fields to interpret each Evidence ID without SQLite.
- Preserves: the existing `NuwaWorkflow.run() -> tuple[skill, quality, report]` contract.

- [ ] **Step 1: Write a failing full Nuwa integration test for self-containment**

  Add `test_full_nuwa_skill_resolves_every_markdown_citation_from_portable_evidence`. Run the real workflow with the existing mock transport, collect citations from all canonical Markdown files, load `references/evidence.json`, and assert every citation resolves to a message with `id`, `sequence`, `sender`, `ts`, and full `text`. The production mutation caught is exporting a selected packet that omits a citation later introduced by persona/Skill rendering.

- [ ] **Step 2: Run the test and verify RED**

  Run: `.venv/bin/pytest -q tests/test_nuwa.py::test_full_nuwa_skill_resolves_every_markdown_citation_from_portable_evidence`

  Expected: FAIL with at least one unresolved citation or missing stable field.

- [ ] **Step 3: Implement portable Evidence closure**

  Before final quality testing, collect citations emitted by `write_skill`, resolve them through the version `EvidenceIndex`, and atomically rewrite `references/evidence.json` with the union of the existing Nuwa packet and cited messages in `sequence` order. Do not query unrelated contacts or widen beyond the current version.

- [ ] **Step 4: Write and verify RED test for copied Skill usability**

  Add `test_copied_canonical_skill_tree_needs_no_private_database`. Copy only `skill/` to a new temporary directory, run `validate_canonical_skill` using its portable message list as the authorized input, and assert canonical/Evidence/Nuwa gates pass. The production mutation caught is a hidden dependency on the original version directory or database.

- [ ] **Step 5: Make rendering and report provenance portable**

  Ensure the Nuwa report stored under `references/` carries the final Skill hash and method/template provenance and that validation can run from the copied tree plus explicit authorized messages. Preserve existing extra files and research checkpoint behavior.

- [ ] **Step 6: Run Nuwa regressions and commit**

  Run: `.venv/bin/pytest -q tests/test_nuwa.py tests/test_nuwa_skill_citations.py tests/test_nuwa_source_recovery.py`

  Expected: PASS.

  Commit:

  ```bash
  git add echotrace/distill/skill_writer.py echotrace/distill/nuwa.py tests/test_nuwa.py
  git commit -m "feat: make nuwa skill self contained"
  ```

### Task 3: Formal Distillation Manifest Builder

**Files:**
- Modify: `echotrace/distill/canonical_skill.py`
- Modify: `tests/test_canonical_skill.py`

**Interfaces:**
- Consumes: internal `manifest`, `parameters`, `metadata` candidate facts, Nuwa report, quality result, job usage, Skill path, canonical gate result, and build time.
- Produces: `build_distill_manifest(*, internal: dict, parameters: dict, metadata: dict, nuwa_report: dict, quality: dict, canonical: dict, skill_path: str, built_at: str) -> dict`.

- [ ] **Step 1: Write a failing literal-field contract test**

  Add `test_formal_manifest_records_v13_required_facts_without_guessing`. Supply hand-built dictionaries and assert exact output for person/session, build time, sources/fingerprints, normalized fingerprint, mode/scope, total/coverage, chunks/failures/fragments, model/Prompt/Nuwa versions, tokenizer/capacity/reserves/usage/approximate, Evidence/Nuwa gates, Skill path, and `complete`. Assert an unavailable optional input remains `None`, not a fabricated default. The production mutation caught is silently guessing absent historical metadata.

- [ ] **Step 2: Run the test and verify RED**

  Run: `.venv/bin/pytest -q tests/test_canonical_skill.py::test_formal_manifest_records_v13_required_facts_without_guessing`

  Expected: FAIL because `build_distill_manifest` does not exist.

- [ ] **Step 3: Implement the pure manifest builder**

  Copy authoritative facts from existing structures, use explicit stable field names, include structured gates/failures, and derive `complete` only from `canonical["complete"]` and `mode == "full"`. The builder performs no database reads and no provider calls.

- [ ] **Step 4: Add legacy/sampled manifest tests**

  Add `test_formal_manifest_keeps_missing_legacy_facts_unknown_and_sampled_incomplete`. Assert sampled remains incomplete even if a caller supplies a truthy canonical result and absent fields remain `None`/empty with a legacy note.

- [ ] **Step 5: Run Task 3 tests and commit**

  Run: `.venv/bin/pytest -q tests/test_canonical_skill.py`

  Expected: PASS.

  Commit:

  ```bash
  git add echotrace/distill/canonical_skill.py tests/test_canonical_skill.py
  git commit -m "feat: build formal distillation manifest"
  ```

### Task 4: Full Completion Gate and Publication

**Files:**
- Modify: `echotrace/distill/pipeline.py`
- Modify: `tests/test_full_chunking.py`

**Interfaces:**
- Consumes: `validate_full_coverage(...)`, Phase 4 internal manifest, final Nuwa/quality output, and Task 1/3 functions.
- Produces: atomic `target/distill_manifest.json`; `metadata.complete` and `metadata.complete_eligible` reflect the final canonical result.
- Preserves: sampled publication and `_active_complete` protection.

- [ ] **Step 1: Write a failing full-pipeline completion test**

  Extend the full mock integration path with `test_valid_full_pipeline_writes_complete_formal_manifest`. Assert `metadata.complete is True`, `distill_manifest.complete is True`, required gates are true, canonical Skill path is relative, and the active published tree contains the formal manifest. The production mutation caught is leaving full permanently incomplete after all Phase 5 gates pass.

- [ ] **Step 2: Run the test and verify RED**

  Run: `.venv/bin/pytest -q tests/test_full_chunking.py::test_valid_full_pipeline_writes_complete_formal_manifest`

  Expected: FAIL because current metadata hard-codes `complete=False` and no formal manifest exists.

- [ ] **Step 3: Integrate final local validation and manifest publication**

  After Nuwa and derived artifacts exist, recompute `validate_full_coverage`, run `validate_canonical_skill`, build/write the formal manifest atomically, and set metadata completion fields from it. Include `distill_manifest.json` in active publication. Do not change Phase 3/4 planning.

- [ ] **Step 4: Write failing tests for stale source and incomplete Nuwa**

  Add `test_source_change_before_publish_records_incomplete_and_stops_formal_publish` and `test_degraded_nuwa_records_incomplete_and_stops_formal_publish`. Mutate the database immediately before the gate in the first; return a failed semantic check in the second. Assert candidate `distill_manifest.json` exists with a named failed gate, metadata is not complete, and no active formal complete version is inserted. The production mutations caught are trusting task-start fingerprints and publishing degraded Nuwa output.

- [ ] **Step 5: Implement explicit Phase 5 failure handling**

  Raise the matching `STALE_DISTILLATION`, `CANONICAL_EVIDENCE_INVALID`, `CANONICAL_SKILL_INCOMPLETE`, or `NUWA_QUALITY_INCOMPLETE` after writing non-sensitive incomplete diagnostics. Keep resumable artifacts in the candidate version.

- [ ] **Step 6: Write and verify sampled compatibility test**

  Add `test_sampled_writes_incomplete_formal_manifest_and_cannot_replace_complete`. Assert preview publication still works when no complete version exists, always records `complete=false`, and the existing guard still rejects replacement of an active complete version.

- [ ] **Step 7: Run Phase 3–5 integration tests and commit**

  Run: `.venv/bin/pytest -q tests/test_full_chunking.py tests/test_canonical_skill.py tests/test_nuwa.py`

  Expected: PASS.

  Commit:

  ```bash
  git add echotrace/distill/pipeline.py tests/test_full_chunking.py
  git commit -m "feat: gate full canonical publication"
  ```

### Task 5: Final Regression and Phase Record

**Files:**
- Modify: `docs/进度.md`

**Interfaces:**
- Consumes: final test/build output.
- Produces: an evidence-based Phase 5 status that separates mock automation from pending real acceptance.

- [ ] **Step 1: Run focused Phase 5 verification**

  Run: `.venv/bin/pytest -q tests/test_canonical_skill.py tests/test_full_chunking.py tests/test_nuwa.py tests/test_nuwa_skill_citations.py tests/test_nuwa_source_recovery.py`

  Expected: PASS with only identified pre-existing warnings.

- [ ] **Step 2: Run the complete Python suite**

  Run: `.venv/bin/pytest -q`

  Expected: every test passes. Any failure is named and resolved or reported; no partial suite is presented as full verification.

- [ ] **Step 3: Run the Web production build**

  Run: `npm run build` in `web/`.

  Expected: TypeScript and Vite build successfully.

- [ ] **Step 4: Run repository checks**

  Run: `git diff --check && git status --short`

  Expected: no whitespace errors; only intentional Phase 5 paths are modified.

- [ ] **Step 5: Update the progress record**

  Record exact commands/counts, mock-only scope, warning details, the canonical/manifest gates now implemented, and the still-unverified real model, human Evidence review, and real QCE conditions. Do not claim Phase 6 or project completion.

- [ ] **Step 6: Commit documentation**

  ```bash
  git add docs/进度.md
  git commit -m "docs: record phase five verification"
  ```

## Plan Self-review

- Spec coverage: canonical tree, self-containment, formal manifest, full-only completion, fingerprints, Evidence, Nuwa quality, derived consistency, sampled compatibility, diagnostics, and acceptance boundaries each map to a task.
- Step scan: every product change is preceded by a named failing behavior test; no production code is scheduled before RED.
- Type consistency: Tasks 2 and 4 consume `validate_canonical_skill` from Task 1; Task 4 consumes `build_distill_manifest` from Task 3; both return dictionaries with `complete`, `gates`, and `failures`.
- Review focus: all five listed failure modes have explicit tests in Tasks 1, 3, or 4.
- Proportion: the plan specifies interfaces and observable assertions without transcribing implementation bodies.
