# Phase 5 Canonical Nuwa Skill Design

## Intent

Phase 5 turns the already verified `full` Map-Reduce output into the canonical,
self-contained Nuwa person Skill required by AGENTS v13. It reuses the existing
Nuwa research, blind-test, refinement, Web preview, versioning, and export
features instead of replacing them. A `sampled` run remains a preview and can
never satisfy the formal completion gate.

The result must make a precise distinction between these states:

- files were generated;
- format checks passed;
- Nuwa semantic checks passed;
- the full source is still current and completely covered;
- all formal gates passed, so the version is `complete`.

Only the final state is eligible for later Phase 6 formal personalized reply.

## Goals

- Preserve the existing Nuwa workflow and its checkpoints.
- Generate the required canonical tree:

  ```text
  skill/
  ├── SKILL.md
  └── references/
      ├── quotes.md
      └── research/
          ├── 01-persona.md
          ├── 02-conversations.md
          ├── 03-expression-dna.md
          ├── 04-external-views.md
          ├── 05-decisions.md
          └── 06-timeline.md
  ```

- Keep existing extra references such as `evidence.json`, `nuwa-report.json`,
  and `workflow.md`; they strengthen compatibility and auditability and do not
  replace the required files.
- Publish `distill_manifest.json` as the formal v13 record while retaining the
  existing internal `manifest.json` for checkpoint compatibility.
- Mark `complete: true` only when every full coverage, Evidence, file,
  fingerprint, and Nuwa quality gate passes.
- Verify that canonical conclusions cite stable Evidence IDs within the
  authorized full scope.
- Ensure derived files do not silently contradict the canonical Skill.
- Preserve sampled behavior and prevent it from overwriting an existing
  complete version.

## Non-goals

- Do not change Phase 3 Whole/Chunked planning or long-message fragmentation.
- Do not change Phase 4 Map-Reduce semantics except where Phase 5 consumes its
  verified output.
- Do not implement Phase 6 three-style personalized reply.
- Do not perform real-provider, real-QCE, or human semantic acceptance.
- Do not redesign the current Web UI or remove existing files and features.
- Do not claim that mechanical Evidence validation proves semantic support.

## Constraints

- `full` must use the already persisted full-scope messages and Phase 4 result;
  Nuwa may select bounded material for individual model calls, but the formal
  manifest must continue to record complete first coverage of the authorized
  input.
- A Nuwa research or validation failure may leave checkpoints and diagnostic
  artifacts, but cannot produce a complete published version.
- Source and normalized-message fingerprints must be recomputed immediately
  before the completion decision, not trusted only from task start.
- No test may contact a real model or QCE. Provider behavior is simulated by
  replacing the client transport.
- Existing sampled, Web/API, SQLite, RAG, versioning, rollback, and export paths
  remain compatible.

## Approaches Considered

### 1. Extend the existing Nuwa workflow (selected)

Reuse `NuwaWorkflow`, `skill_writer.py`, version publication, and current
quality reports. Add explicit canonical validation and manifest completion
gates around them. This avoids maintaining two Skill formats and preserves the
working UI and historical versions.

### 2. Build a separate full-only Skill pipeline

This creates a clean isolation boundary but duplicates research, rendering,
quality checks, checkpoints, and publication. The two output formats would be
likely to drift and existing Web behavior would require parallel maintenance.

### 3. Add only a final `complete` boolean

This is smaller but treats generated files as proof of correctness. It cannot
establish self-containment, current fingerprints, Evidence scope, or derived
data consistency, so it does not satisfy v13.

## Architecture

### Canonical validator

A focused validator will inspect the completed candidate version before
publication. It receives the database, contact, target directory, internal
manifest, Nuwa report, and quality report. It returns a structured gate report
instead of relying on one broad boolean.

The report covers:

1. `mode_full`: the run is explicitly `full`.
2. `coverage_complete`: Phase 4 succeeded, all chunks succeeded, and full
   coverage validation still passes.
3. `source_current`: stored source and normalized-message fingerprints match
   freshly computed current scoped messages.
4. `canonical_files`: every required Skill/research file exists and is
   non-empty.
5. `evidence_valid`: citations parsed from canonical Markdown exist in the
   current authorized Evidence set; research entries preserve their checked
   source type and IDs.
6. `nuwa_quality`: format checking and Nuwa semantic validation both passed,
   and the validated Skill hash matches the current `SKILL.md`.
7. `derived_consistent`: derived persona/quotes/metrics remain present and do
   not introduce out-of-scope Evidence; canonical Skill remains the authority.

`complete` is the conjunction of all gates. The report records failures
individually so an incomplete result is diagnosable.

### Formal manifest

`distill_manifest.json` is written atomically after candidate artifacts exist
and before publication. It consolidates existing planner and task metadata:

- person/session identifier and build time;
- input sources, source file fingerprints, and normalized message fingerprint;
- `full` or `sampled`, authorized scope, total and first-covered counts;
- chunk count, success/failure counts, and long-message fragment count;
- provider/model, Prompt hashes, Nuwa method/template version;
- tokenizer/estimator, context capacity and source, output reserve, safety
  margin, actual/estimated usage, and `approximate`;
- Evidence result, Nuwa result, canonical Skill path, structured gates, and
  final `complete`.

Fields already stored in the internal manifest, parameters, job usage, and
Nuwa report are copied rather than independently guessed. Missing historical
fields are explicit `null`/unknown for compatibility, never fabricated.

### Publication behavior

- Successful full run with every gate passing: write
  `distill_manifest.json`, set `metadata.complete=true`, then publish.
- Full run with a Phase 5 gate failure: preserve candidate diagnostics and the
  formal manifest with `complete=false`; stop before making it the active
  formal version.
- Sampled run: continue the existing preview publication, write a formal
  manifest marked sampled/incomplete, and never replace an active complete
  version.
- Existing versions without `distill_manifest.json` remain readable but are
  treated as legacy/incomplete for formal use.

### Self-containment and Evidence

`SKILL.md`, quotes, and six research documents retain nearby Evidence IDs and
plain-language limitations. `references/evidence.json` remains as the portable
Evidence summary needed to interpret those IDs without the EchoTrace database.
The validator checks that every canonical citation resolves inside this
portable evidence packet and the current authorized scope.

This is a structural and provenance guarantee. Whether the cited text truly
supports a conclusion still requires Phase 8 human review with a real model.

### Derived-data consistency

Canonical Skill is authoritative. Phase 5 does not attempt a second semantic
model comparison between Markdown and every JSON derivative. It enforces the
mechanically provable subset:

- required derived artifacts exist;
- Evidence IDs in derived artifacts stay within the same authorized scope;
- the Skill hash in the Nuwa validation report matches current bytes;
- sampled or stale derivatives cannot make a full Skill complete.

Any stronger semantic equivalence claim is deferred to real-model and human
acceptance rather than simulated by string matching.

## Error and Recovery Behavior

- Missing canonical file: `CANONICAL_SKILL_INCOMPLETE` with failed-file gates.
- Stale source or normalized fingerprint: `STALE_DISTILLATION` and
  `complete=false`.
- Out-of-scope or missing citation: `CANONICAL_EVIDENCE_INVALID`.
- Failed/stale Nuwa validation: `NUWA_QUALITY_INCOMPLETE`.
- Resume reuses existing paid Nuwa checkpoints only when their established
  parameter/fingerprint contracts still match; the final gates always rerun
  locally before publication.

Errors preserve non-sensitive diagnostics. They do not log chat text, API keys,
or real provider responses.

## Testing Strategy

Every implementation slice follows RED→GREEN.

- A full mock run produces every canonical file and a self-contained portable
  Evidence packet.
- Removing or emptying any required file prevents completion.
- A citation outside the authorized scope prevents completion.
- A changed source/normalized fingerprint prevents completion immediately
  before publication.
- Format failure, semantic failure, stale Skill hash, and incomplete Phase 4
  each prevent completion.
- A valid full run writes all required manifest fields and becomes complete.
- A sampled run remains incomplete and cannot replace a complete full version.
- Existing Nuwa, Web/API, versioning, export, Phase 3, and Phase 4 tests remain
  green.
- Final verification runs the entire Python suite, the Web production build,
  and `git diff --check`.

## Acceptance Boundary

Phase 5 is complete when mock/offline automation proves canonical file
generation, self-containment, Evidence scope, structured manifest recording,
Nuwa gates, stale detection, and full/sampled publication behavior without
regressing existing capabilities.

It still does not prove real-model character quality or real QCE compatibility,
and it does not enable formal personalized reply; those remain Phase 6 and
Phase 8 work.
