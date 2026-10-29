# EchoTrace Phase 2 Dynamic Request Budget Design

## Intent

Phase 2 makes request admission inspectable before any model call. EchoTrace must count the complete request, reserve output and safety space, explain where model capacity came from, and provide a dry-run that does not read secrets, connect to a provider, expose chat text, or write distillation artifacts.

This phase prepares reusable budget and diagnostic planning interfaces for Phase 3. It does not replace the current production sampled/fixed-chunk executor with Whole/Chunked execution, and it does not create `distill_manifest.json` or formal reply behavior.

## Goals

- Resolve context and output capacity from verified bundled metadata or an explicitly confirmed user value; never guess an unknown model's capacity.
- Count the serialized request components: system instructions, rendered stage prompt, JSON schema/response constraint, roles and message metadata, output reserve, reasoning allowance, and safety margin.
- Prefer a matching tokenizer when one is installed; otherwise use a deterministic conservative estimator and set `approximate: true`.
- Produce a deterministic, read-only dry-run through Web/API and CLI before API-key/Base-URL validation.
- Report Whole versus estimated Chunked, sequence ranges, new-message/fragment counts, overlap counts, capacity source, estimator, actual planned input use, and remaining capacity.
- Reuse the same request-accounting and diagnostic packing interfaces in Phase 3 rather than maintaining a second formula.

## Non-goals

- No production Whole/Chunked execution, overlap policy switch, long-message fragment persistence, Map-Reduce rewrite, full/sampled publication gate, or canonical manifest.
- No provider call, model-list probe, tokenizer download, API-key read, chat-body logging, or Skill/version write during dry-run.
- No claim that estimated token counts equal provider billing or that bundled metadata applies to third-party services reusing a model name.

## Approaches considered

### A. Verified metadata plus explicit fallback — selected

Bundle a deliberately small model capability registry backed by official documentation. Exact IDs `deepseek-flash` and `deepseek-v4-pro` resolve to a 1,048,576-token context and 393,216-token maximum output. Unknown IDs require the user to confirm the configured context window. Capacity records include source name and retrieval date; third-party aliases are not inferred.

This provides safe defaults for known official endpoints without making stale or ambiguous guesses. The registry is data, not network discovery, so dry-run stays offline.

### B. Explicit configuration only

Simpler and provider-neutral, but makes every user repeat known official metadata and gives no useful automatic source record.

### C. Large tokenizer/model registry

Potentially more exact, but adds dependencies and a fast-moving compatibility surface. It is disproportionate for Phase 2 and could falsely equate third-party aliases with official services.

## Architecture

### Model capability resolution

Create `echotrace/llm/capabilities.py` with immutable capability records and `resolve_capability(model, configured_context, configured_output, confirmed)`. Exact bundled IDs win only when the configured Base URL is the official provider host; otherwise the explicitly confirmed values win. Unknown or non-official combinations without confirmation raise `MODEL_CAPACITY_REQUIRED` before request construction.

Settings add `context_window_confirmed: bool`, defaulting to false. The UI explains that checking it means the configured provider really supports the stated total input-plus-output capacity. Existing real calls retain their current flow only after either verified metadata resolves or the user confirms the value.

### Token counting

Create `echotrace/llm/tokenizer.py` with a small estimator interface returning `{tokens, name, approximate}`. A matching locally installed tokenizer may register itself; no tokenizer is downloaded or imported as a mandatory dependency. The fallback estimator is deterministic and conservative and remains clearly approximate.

`echotrace/llm/budget.py` becomes the single request-accounting module. A `RequestBudget` result records:

- stage and model;
- context/output capacity and their source;
- estimator name and `approximate`;
- static request tokens, content tokens, requested output reserve, reasoning reserve, safety margin;
- available input, planned input, and remaining input;
- admission result and a stable diagnostic error code.

The safety margin is `max(1024, ceil(context_capacity * 0.01))`. Output reserve is the stage target capped by both configured and model output limits; adapter thinking tokens are part of that output reserve, never extra capacity. `available_input = context_capacity - static_request - output_reserve - safety_margin`.

The existing `allowance()` delegates to this accounting result so dry-run and real calls cannot silently use different formulas.

### Dry-run planning

Create `echotrace/distill/dry_run.py`. It reads canonical messages in `sequence` order and builds the same complete Map request envelope used by the gateway. If all authorized messages fit, the result is `Whole`. Otherwise it greedily estimates chunks against `available_input`, preserving order and maximizing each chunk. Phase 2 uses zero overlap and input-view-only long-message fragments; the output labels these as diagnostic estimates. Phase 3 will add and execute the final overlap policy without changing request accounting.

Each reported chunk includes sequence start/end, new messages, new fragments, overlap messages (`0` in Phase 2), estimated request tokens, and whether any long message was fragmented. Fragment text and chat body are never returned by the API or CLI.

The dry-run function must operate on configuration returned without secrets. It does not call `LLMClient.settings()`, because that method reads secrets and validates Base URL/privacy state. It writes no job, token usage, review file, checkpoint, version, Skill, or manifest.

### API, CLI, and Web

- `POST /api/contacts/{cid}/distill/dry-run` accepts model, thinking, scope, mode intent, and capacity confirmation fields but no consent requirement.
- The response contains only counts, IDs/ranges, token diagnostics, capability source, and approximation flags.
- `echotrace distill --dry-run --contact …` calls this endpoint, does not require `--consent`, and exits non-zero on configuration/capacity failure.
- The distillation dialog gets a “预算预检” action and renders the non-sensitive result. Starting a real task remains a separate consented action.

## Errors and privacy boundaries

- `MODEL_CAPACITY_REQUIRED`: unknown/ambiguous provider-model capacity was not explicitly confirmed.
- `REQUEST_OVERHEAD_TOO_LARGE`: prompt/schema/output/safety overhead leaves no input capacity.
- `MESSAGE_FRAGMENT_UNPLANNABLE`: even the minimum non-empty fragment cannot fit.
- Existing `LLM_OUTPUT_BUDGET_TOO_SMALL` and `LLM_CONTEXT_TOO_LARGE` remain compatible at the live gateway boundary.

Tests replace HTTP transports with fail-fast sentinels and make the secret store unreadable to prove dry-run does not touch either. They snapshot the data/Skill tree before and after dry-run to prove zero artifact writes. API responses are checked to ensure no original message text or key appears.

## Compatibility and migration

- Existing settings load with `context_window_confirmed=false`; known official models can resolve from bundled metadata, while unknown/third-party models require one explicit confirmation.
- Current quick/standard/deep and fixed 10,000-token production chunking remain unchanged in this phase.
- Existing recovery identity remains unchanged; budget diagnostics do not invalidate or resume old jobs.
- Bundled metadata is intentionally small and dated. Updating it requires an official primary source and regression tests.

## Verification boundary

Automated tests cover exact request-component sensitivity, output/reasoning reserve changes, known metadata, unknown-model rejection, approximate fallback, deterministic Whole/Chunked diagnostics, sequence ranges, fragment reconstruction, no network, no key read, no body disclosure, and zero artifact writes. Real provider tokenizer parity and actual provider acceptance remain unverified until Phase 8.
