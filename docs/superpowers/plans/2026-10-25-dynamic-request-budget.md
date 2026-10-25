# Dynamic Request Budget Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add verified model-capacity resolution, complete-request accounting, and a privacy-safe offline dry-run for EchoTrace Phase 2.

**Architecture:** A small capability registry resolves official model limits or requires an explicitly confirmed user limit. A tokenizer abstraction and a single request-budget module serve both the existing live gateway and a read-only canonical-message dry-run planner; API, CLI, and Web only expose non-sensitive diagnostics.

**Tech Stack:** Python 3.11, FastAPI, Pydantic, SQLite, React/TypeScript, pytest, existing deterministic JSON serialization.

**Spec:** `docs/superpowers/specs/2026-10-25-dynamic-request-budget-design.md`

## Global Constraints

- Implement only v13 Phase 2; do not switch production distillation to Whole/Chunked or implement Phase 3 overlap execution.
- Dry-run must run before API-key, Base-URL, privacy-consent, or connectivity validation; it must not read secrets, connect to a provider, emit chat text, or write jobs/usage/review/Skill/version/manifest files.
- Unknown or third-party provider/model capacities must be explicitly confirmed; never infer official limits from a similar model name.
- Count the complete serialized request, including system instruction, rendered prompt, schema/response constraint, roles/metadata, output reserve, reasoning allowance, and safety margin.
- Tokenizer fallback must be deterministic and marked `approximate: true`; no tokenizer or model file may be downloaded.
- Preserve existing live-call recovery identities, output-growth behavior, quick/standard/deep execution, and current published artifacts.
- Use only fictional fixtures and mock transports; do not call a real provider or QCE.
- Stage only named files; do not commit secrets, databases, logs, generated chat data, or frontend dependency/build directories.

## Review Focus

- Official model ID through a non-official Base URL must require explicit capacity confirmation rather than inherit official limits — Task 1 test.
- Changing prompt/schema/output reserve/reasoning settings must change available input and admission consistently — Task 2 tests.
- A static request whose overhead alone fills the context must fail immediately without an empty chunk or loop — Task 2 test.
- One oversized message must produce continuous non-empty diagnostic fragments that reconstruct exactly and retain one Evidence ID — Task 3 test.
- Dry-run must remain zero-network, zero-secret-read, zero-artifact-write, and must not disclose message text even on errors — Task 4 tests.

---

### Task 1: Verified model capability resolution

**Files:**
- Create: `echotrace/llm/capabilities.py`
- Modify: `echotrace/config.py`
- Modify: `web/src/WorkspacePages.tsx`
- Modify: `web/src/zh-CN.ts`
- Test: `tests/test_request_budget.py`
- Test: `tests/test_output_limit.py`

**Interfaces:**
- Produces: `ModelCapability`, `ResolvedCapability`, and `resolve_capability(model, base_url, configured_context, configured_output, confirmed)`.
- Produces: persisted `llm.context_window_confirmed: bool` with a Web control and explanation.
- Consumes later: Tasks 2–4 use `ResolvedCapability.context_window`, `max_output_tokens`, `source`, and `verified`.

- [ ] **Step 1: Write failing capability and settings tests**

Cover exact official DeepSeek IDs on `https://api.deepseek.com`, official IDs through a third-party host, unknown IDs, explicit confirmation, output-limit capping, and migration of settings without `context_window_confirmed`.

- [ ] **Step 2: Run the new tests and verify RED**

Run: `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest -p pytest_asyncio.plugin -q tests/test_request_budget.py tests/test_output_limit.py -k 'capability or context_window_confirmed'`

Expected: imports/fields do not exist or unknown capacities are still silently accepted.

- [ ] **Step 3: Implement immutable capability records and exact-host resolution**

Add only `deepseek-flash` and `deepseek-v4-pro`, each with context `1048576`, max output `393216`, official host `api.deepseek.com`, source label `deepseek-official-2026-10-25`. Do not alias `deepseek-v4.1-flash`.

- [ ] **Step 4: Persist and render explicit capacity confirmation**

Add the boolean setting with default false, Pydantic validation, checkbox UI, and Chinese warning. Existing settings without the field load as false.

- [ ] **Step 5: Run Task 1 tests and verify GREEN**

Run: `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest -p pytest_asyncio.plugin -q tests/test_request_budget.py tests/test_output_limit.py -k 'capability or context_window_confirmed'`

Expected: all selected tests pass.

- [ ] **Step 6: Commit named Task 1 paths**

Commit message: `feat: resolve verified model capacities`

### Task 2: Complete-request accounting and live gateway compatibility

**Files:**
- Create: `echotrace/llm/tokenizer.py`
- Modify: `echotrace/llm/budget.py`
- Modify: `echotrace/llm/client.py`
- Test: `tests/test_request_budget.py`
- Test: `tests/test_large_distill.py`
- Test: `tests/test_extraction_output_growth.py`

**Interfaces:**
- Consumes: Task 1 `ResolvedCapability`.
- Produces: `TokenCount(tokens, name, approximate)`, `count_tokens(value, model)`, `RequestBudget`, and `plan_request_budget(stage, settings, request, attempt=0)`.
- Preserves: `allowance(stage, settings, estimate, request, attempt=0) -> tuple[int, int]` as a compatibility wrapper used by existing live calls.
- Consumes later: Task 3 uses `RequestBudget.available_content_tokens` and request-envelope accounting.

- [ ] **Step 1: Write failing request-budget tests**

Assert component totals and sensitivity to system instructions, schemas, message metadata, output targets, adapter thinking budgets, safety margin `max(1024, ceil(context * .01))`, provider output ceiling, approximate fallback, and deterministic repeated results.

- [ ] **Step 2: Add failing overhead-exhaustion and unknown-capacity tests**

Assert `REQUEST_OVERHEAD_TOO_LARGE` before any provider transport and `MODEL_CAPACITY_REQUIRED` for unconfirmed unknown models.

- [ ] **Step 3: Run Task 2 tests and verify RED**

Run: `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest -p pytest_asyncio.plugin -q tests/test_request_budget.py tests/test_large_distill.py::test_full_request_window_and_stage_output_ceiling`

Expected: structured diagnostics are missing and the existing allowance uses the old fixed formula.

- [ ] **Step 4: Implement tokenizer abstraction and structured request accounting**

Keep the fallback estimator behavior deterministic; expose its name and approximation flag. Separate static envelope tokens from supplied content tokens by building the same serialized request with the stage context content replaced by its empty structural form.

- [ ] **Step 5: Delegate existing `allowance()` to the new budget result**

Map structured errors to the existing live gateway error codes/messages where compatibility tests require them; do not change checkpoint identity or retry counts.

- [ ] **Step 6: Run live admission and output-growth regressions**

Run: `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest -p pytest_asyncio.plugin -q tests/test_request_budget.py tests/test_large_distill.py::test_full_request_window_and_stage_output_ceiling tests/test_extraction_output_growth.py`

Expected: all selected tests pass.

- [ ] **Step 7: Commit named Task 2 paths**

Commit message: `feat: account for complete model requests`

### Task 3: Deterministic read-only dry-run planner

**Files:**
- Create: `echotrace/distill/dry_run.py`
- Modify: `echotrace/ingest/canonical.py`
- Test: `tests/test_distill_dry_run.py`

**Interfaces:**
- Consumes: `canonical_messages(db, contact_id)` and Task 2 request budget.
- Produces: `dry_run_distill(db, config, llm, contact_id, options) -> dict` containing mode, source counts/fingerprint, capability/estimator metadata, non-sensitive request diagnostics, and chunk descriptors.
- Produces chunk fields: `index`, `sequence_start`, `sequence_end`, `new_messages`, `new_fragments`, `overlap_messages`, `estimated_request_tokens`, `fragmented_messages`.
- Consumes later: Task 4 exposes this function unchanged through API/CLI/Web; Phase 3 reuses its packing primitives.

- [ ] **Step 1: Write failing Whole and deterministic Chunked tests**

Assert capacity changes alter the decision and boundaries; identical inputs/configuration produce byte-identical diagnostics; prompt/schema changes change boundaries; no fixed message-count or 10,000-token cap is applied.

- [ ] **Step 2: Write failing long-message and forward-progress tests**

Assert non-empty continuous fragments reconstruct the original text exactly, retain one `mNNNNNN` ID, first coverage occurs exactly once, each chunk advances, zero overlap is reported, and impossible static overhead fails without emitting chunks.

- [ ] **Step 3: Run Task 3 tests and verify RED**

Run: `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest -p pytest_asyncio.plugin -q tests/test_distill_dry_run.py`

Expected: module/function missing.

- [ ] **Step 4: Implement request-envelope construction and greedy diagnostic packing**

Use source `sequence`, maximize each chunk within available content capacity, split only the model input view, and keep fragment text internal. Return counts/ranges only.

- [ ] **Step 5: Run Task 3 tests and verify GREEN**

Run: `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest -p pytest_asyncio.plugin -q tests/test_distill_dry_run.py`

Expected: all tests pass.

- [ ] **Step 6: Commit named Task 3 paths**

Commit message: `feat: plan distillation without external calls`

### Task 4: Dry-run API, CLI, and Web entry points

**Files:**
- Modify: `echotrace/api/routes.py`
- Modify: `echotrace/cli.py`
- Modify: `web/src/DistillDialog.tsx`
- Modify: `web/src/zh-CN.ts`
- Test: `tests/test_distill_dry_run.py`
- Test: `tests/test_cli.py`
- Test: `tests/test_boundaries.py`

**Interfaces:**
- Consumes: Task 3 `dry_run_distill`.
- Produces: `POST /api/contacts/{cid}/distill/dry-run` and `echotrace distill --dry-run`.
- Produces: Web budget-preview action and non-sensitive diagnostic display; real start remains separate and consent-gated.

- [ ] **Step 1: Write failing privacy-boundary API tests**

Make `.secrets.json` unreadable or monkeypatch secret reads to fail, install a transport that raises on use, snapshot database/artifact paths, and assert dry-run succeeds without consent while response/error JSON excludes message text and secret values.

- [ ] **Step 2: Write failing CLI tests**

Assert `distill --dry-run --contact ID` does not require `--consent`, calls only the dry-run endpoint, prints diagnostics, and exits non-zero for capacity/configuration errors.

- [ ] **Step 3: Run API/CLI tests and verify RED**

Run: `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest -p pytest_asyncio.plugin -q tests/test_distill_dry_run.py tests/test_cli.py tests/test_boundaries.py -k 'dry_run or consent'`

Expected: route and flag do not exist.

- [ ] **Step 4: Add typed API payload and call the planner without `LLMClient.settings()`**

Validate contact and scope locally; do not create a job or invoke provider/config secret paths.

- [ ] **Step 5: Add CLI dry-run and Web budget preview**

Keep result fields non-sensitive and use the existing local API helper/error display patterns.

- [ ] **Step 6: Run API/CLI/privacy tests and production build**

Run: `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest -p pytest_asyncio.plugin -q tests/test_distill_dry_run.py tests/test_cli.py tests/test_boundaries.py`

Run: `python scripts/build.py`

Expected: tests and build exit 0; use Linux Node/npm PATH in WSL.

- [ ] **Step 7: Commit named Task 4 paths**

Commit message: `feat: expose private distillation dry run`

### Task 5: Documentation and Phase 2 verification

**Files:**
- Modify: `README.md`
- Modify: `docs/进度.md`
- Modify: `docs/快速启动提示词.md`
- Modify: `docs/superpowers/plans/2026-10-25-dynamic-request-budget.md`

**Interfaces:**
- Documents verified Phase 2 behavior, official metadata source/date, approximation semantics, and unchanged Phase 3+ gaps.

- [ ] **Step 1: Document settings confirmation, API/CLI dry-run, and privacy guarantees**

State that diagnostic Chunked boundaries are not yet the production executor and that real tokenizer/provider parity remains unverified.

- [ ] **Step 2: Run Phase 1+2 targeted regressions**

Run: `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest -p pytest_asyncio.plugin -q tests/test_core_messages.py tests/test_request_budget.py tests/test_distill_dry_run.py tests/test_cli.py tests/test_boundaries.py tests/test_extraction_output_growth.py tests/test_empty_quote_recovery.py tests/test_nuwa_source_recovery.py tests/test_reported_observations.py tests/test_resume_budget.py`

Expected: zero failures; report warnings separately.

- [ ] **Step 3: Attempt the complete Python suite and report the exact outcome**

Run: `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest -p pytest_asyncio.plugin -q`

Expected: zero failures. If the previously recorded background-statistics stall recurs, preserve the timeout evidence and do not claim a full-suite pass.

- [ ] **Step 4: Run production build and repository integrity checks**

Run: `PATH=/usr/local/bin:/usr/bin:/bin python scripts/build.py`

Run: `git diff --check && git status --short`

Expected: build exits 0; no whitespace errors; only intended named paths remain.

- [ ] **Step 5: Commit documentation and verified final state**

Commit message: `docs: record dynamic budget dry run`

- [ ] **Step 6: Push the existing feature branch without merging**

Run: `git push origin feature/canonical-message-evidence`

Expected: remote branch advances; `main` remains unchanged.
