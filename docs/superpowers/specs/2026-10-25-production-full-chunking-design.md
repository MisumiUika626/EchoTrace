# EchoTrace Phase 3 Production Full/Chunked Design

## Intent

Phase 3 turns the Phase 2 read-only Whole/Chunked planner into the production input planner for a new formal `full` distillation mode. `full` covers every authorized source message body exactly once on first coverage and uses the same complete-request accounting as dry-run. Existing `quick` / `standard` / `deep` behavior remains available as an explicitly incomplete `sampled` preview path.

This phase changes input selection and chunk persistence only. It does not implement the Phase 4 open-topic Map/Reduce and Evidence contract, the Phase 5 canonical Skill/`distill_manifest.json` complete gate, or the Phase 6 three-style reply contract.

## Confirmed product choices

- Use one shared planner for dry-run and production full execution; there must not be a second production budget formula.
- New Web/API starts default to `full` for the course-core path.
- Existing queued/resumable jobs whose payload has no `mode` remain legacy `sampled`; deployment must not reinterpret them as full.
- `sampled` retains `quick` / `standard` / `deep`, is visibly marked preview/incomplete, and is not eligible for a future complete canonical Skill.
- Adjacent full chunks may copy at most three complete messages from the previous chunk and at most 5% of the new chunk's available content capacity. Oldest overlap is removed first; overlap falls to zero when necessary for forward progress.

## Goals

- Execute either Whole or deterministic maximum-fill Chunked input planning from the exact same planner used by dry-run.
- Preserve canonical `sequence`, Evidence ID, source order, complete permanent text, and deterministic boundaries.
- Guarantee every authorized message or long-message fragment has first coverage exactly once.
- Split an oversized message only in the model-facing input view; fragments are continuous, non-empty, share one Evidence ID, and reconstruct the permanent source text byte-for-byte as Unicode text.
- Persist enough non-sensitive planning metadata to resume safely and audit source coverage without introducing the formal Phase 5 manifest.
- Keep current worker-pool, extraction recovery, cache, version, RAG, Nuwa, and publication paths working.

## Non-goals

- No recursive Reduce framework, new observation schema, semantic Evidence support check, `complete=true`, canonical Skill rewrite, or formal Reply implementation.
- No migration or rewriting of already completed version directories.
- No claim that a sampled preview, summary-only run, mock provider, or dry-run is a complete portrait.
- No deletion of the legacy sampled preparation path in this phase.

## Approaches considered

### A. Shared planner as the only full boundary source — selected

Refactor the Phase 2 planner into a pure reusable planning module. Dry-run sanitizes its plan; production persists and executes the same plan. This keeps boundary decisions reproducible when prompts, schemas, output reserves, model metadata, or token estimation change.

### B. Reimplement dynamic packing inside `Pipeline.prepare()`

This keeps changes local but creates two formulas. A preview could then disagree with the real request, defeating the privacy-safe capacity check.

### C. Split the existing fixed 10,000-token chunks again

This is easiest to retrofit but cannot meet maximum-fill or remove the fixed small chunk ceiling, so it is not formal full mode.

## Mode and compatibility contract

`DistillStart` gains `mode: "full" | "sampled"`, default `full`. `tier` remains meaningful only for sampled mode. The Web dialog defaults to full and presents sampled as a clearly incomplete cost-control option with the three existing tiers.

The pipeline resolves mode as follows:

- explicit payload `mode=full`: production full planner;
- explicit payload `mode=sampled`: legacy sampling planner;
- missing `mode`: legacy sampled compatibility for jobs created before this field existed.

The resolved mode, tier, planner version, request-capacity inputs, source fingerprint, prompt/schema identity, and approximation flag become checkpoint identity. Changing any of them rejects resume rather than silently reusing incompatible chunks.

Full requires raw authorized message bodies. `mode=full` combined with summary-only/privacy settings that prevent raw text raises `FULL_REQUIRES_RAW_TEXT` before writing chunk inputs. Summary-only remains available through sampled preview. This prevents placeholders from being called complete source coverage.

Phase 3 writes `distill_mode`, `coverage_mode`, `coverage_ratio`, `first_covered_messages`, `first_covered_fragments`, `fragmented_messages`, and `complete_eligible: false` into version metadata. `complete_eligible` remains false because Phase 4/5 gates do not yet exist. A sampled run must not replace a future active version whose metadata says `complete: true`; the guard is added now for forward compatibility.

## Shared planning model

Move reusable packing from the dry-run presentation module into a focused production-neutral module. The plan contains immutable chunk records with:

- ordered model-facing `views`;
- which views are new first coverage versus overlap;
- complete-request `RequestBudget`;
- sequence start/end;
- new message and new fragment counts;
- overlap message count;
- fragmented Evidence IDs.

The planner accepts canonical rows, LLM request-envelope builder inputs, settings, and overlap policy. Because every raw full chunk is sent to both `distill_chunk` and `extract_quotes`, a candidate fits only when both complete stage requests fit; the tighter stage determines the boundary. Stage budgets are recorded separately. The request context, including the target display name used by production, is identical in dry-run planning but never returned in diagnostics. The planner never reads secrets, calls a provider, or writes files. Dry-run converts the plan to its existing body-free diagnostics. Production writes the exact planned views to `inputs/*.json` and the body-free descriptors to the existing version `manifest.json`.

## Maximum fill and overlap

For each chunk, the planner starts with eligible overlap from the previous chunk, then binary-searches the largest consecutive prefix of unseen units that fits the complete request. Except for the final chunk, attempting to add the next unseen unit must exceed capacity.

Overlap rules:

- only complete, unfragmented messages may overlap;
- candidates are the previous chunk's last at most three complete messages;
- their estimated content cost must not exceed 5% of the receiving chunk's available content capacity;
- overlap contributes to request capacity but never to first-coverage counts;
- if no unseen unit fits, remove overlap from oldest to newest and retry, eventually reaching zero;
- every emitted chunk contains at least one unseen message or unseen fragment.

The 5% check uses the same deterministic estimator metadata as request planning. It is a ceiling, not a target; fewer or zero messages are valid.

## Long-message fragments

When one unseen complete message cannot fit an empty data chunk, binary-search the longest non-empty text prefix that fits. Continue until the entire text is represented. Each view retains the same `id`, `sequence`, sender, timestamp, and kind, and adds zero-based `fragment_index`, total `fragment_count`, and character-based `fragment_offset`.

Fragments never overlap. Concatenating their text by `fragment_index` must exactly equal the canonical permanent message text. Production input files may contain fragments, but SQLite messages and canonical source rows are never modified.

Evidence indexing must not let repeated fragment IDs overwrite or truncate the permanent message. Before building `evidence.sqlite`, a coverage validator reads the current canonical rows for the contact, reconstructs all first-coverage fragments, and compares them with the authorized source snapshot and fingerprint; the index then inserts one full canonical payload per Evidence ID rather than a fragment. Overlap copies are ignored for reconstruction and counts. Any gap, duplicate first coverage, offset mismatch, or text mismatch raises `INVALID_FULL_COVERAGE` before aggregate stages or publication.

## Production persistence and execution

Full preparation writes to the existing version staging directory:

- `inputs/NNNNN.json`: exact ordered model-facing views for that request, including bounded overlap;
- `manifest.json`: legacy fields needed by current consumers plus planner version, Whole/Chunked, capacity/estimator metadata, source fingerprint, chunk descriptors, and coverage totals;
- no formal `distill_manifest.json` yet.

The existing fixed worker pool processes these files. Observation and quote extraction continue using their current schemas and recovery behavior. Duplicate outputs caused by overlap remain deduplicated by the existing evidence/value identities; overlap IDs are allowed for validation but excluded from coverage totals.

Whole is represented as one input file with every authorized canonical message and no overlap. The production `distill_chunk` and `extract_quotes` request envelopes for each persisted input must each recalculate to their recorded budgets before provider use; mismatch raises `PLANNER_REQUEST_MISMATCH` rather than sending an unexpectedly larger request.

Legacy sampled preparation stays byte-compatible where practical so existing caches and checkpoints continue to resume. New sampled versions add only labeling metadata and UI warnings; their old fixed 10,000-token behavior is intentionally not presented as dynamic full.

## API, CLI, and Web

- API start payload accepts `mode`; dry-run accepts the same mode intent but always plans full coverage when `mode=full`.
- CLI adds `--mode full|sampled`, defaulting to full for new commands; `--tier` applies only to sampled.
- Web shows “完整蒸馏（课程核心）” selected by default and “抽样预览（不完整）” as the alternative. Tier controls appear only for sampled.
- Dry-run and start submit the same mode, model, thinking, scope, and raw/summary intent so their boundaries match.
- Results and version summaries display full/Whole, full/Chunked, or sampled preview rather than only “N 条采样”.

## Errors and safety

- `FULL_REQUIRES_RAW_TEXT`: full requested while raw bodies are disabled.
- `INVALID_FULL_COVERAGE`: missing, duplicated, reordered, or non-reconstructable first coverage.
- `PLANNER_REQUEST_MISMATCH`: persisted input no longer matches its recorded request budget or planner identity.
- Existing `MODEL_CAPACITY_REQUIRED`, `REQUEST_OVERHEAD_TOO_LARGE`, and output-budget errors remain unchanged.

No new real-provider or QCE access is introduced. Tests use fictional messages and mock transports. Input files remain local artifacts with the same privacy sensitivity as imported chat and must not be committed.

## Verification

Automated tests must cover:

- Whole and Chunked production boundaries matching dry-run exactly, with the tighter of `distill_chunk` and `extract_quotes` controlling each raw full boundary;
- changes in context, Prompt, schema, output reserve, and reasoning settings changing both paths identically;
- maximum fill, strict sequence, overlap limits, oldest-first shrink, zero-overlap fallback, and guaranteed progress;
- first coverage exactly once for every authorized message/fragment;
- long-message offsets and exact reconstruction into one full Evidence record;
- scoped full coverage and source fingerprint stability;
- full/raw rejection and sampled/summary compatibility;
- new full default, legacy missing-mode sampled resume, checkpoint mismatch rejection, and future complete-version protection;
- existing worker pool, extraction recovery, incremental sampled cache, Web/API/CLI, full Python suite, and production frontend build.

Real tokenizer parity, real provider acceptance, semantic quality, formal complete publication, and real QCE remain later acceptance work.
