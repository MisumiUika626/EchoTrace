# Full Map-Reduce Evidence Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为 full 蒸馏增加开放主题 Map、受限 Reduce、Evidence 验证和可恢复的不完整状态。

**Architecture:** full 在既有动态分片之后进入独立 Phase 4 管线；sampled 保持旧路径。新模块负责确定性验证、去重和最多一层中间归并，pipeline 只负责调用和持久化状态。

**Tech Stack:** Python 3.11、Pydantic、pytest、现有 LLMClient 与 JSON 检查点。

**Spec:** `docs/superpowers/specs/2026-10-25-full-map-reduce-evidence-design.md`

## Global Constraints

- 仅实现 Phase 4，不提前实现 canonical Skill、正式回复或 `complete=true`。
- full 使用 Phase 3 的动态容量和分片；不得加入固定消息数或固定 10,000 token 裁决。
- sampled 行为保持兼容。
- 每项实质结论必须有授权范围内 Evidence；机械验证不冒充语义验证。
- 所有模型测试离线，不发送真实聊天或密钥。

## Review Focus

- 重叠输入产生相同观察时只能保留一份，且不能改变首次覆盖统计。
- 仅由本人发言支持的对方结论不能伪装成 direct。
- 中间 Reduce 后最终请求仍超限必须停止，不能递归。
- 进程中断后不得重做已成功 Map 或 Reduce 检查点。
- sampled 不应调用 full_map/full_reduce，也不改变旧产物。

---

### Task 1: 开放主题合同与 Evidence 验证

**Files:**
- Create: `echotrace/distill/full_reduce.py`
- Modify: `echotrace/llm/schemas.py`
- Create: `prompts/full_map.md`
- Create: `prompts/full_reduce.md`
- Test: `tests/test_full_reduce.py`

**Interfaces:**
- Consumes: Phase 3 model-message 字典及授权 Evidence ID 集合。
- Produces: `validate_findings(document, messages, allowed_ids, owned_ids=None)` 与 `deduplicate_findings(items)`。

- [ ] **Step 1: Write the failing tests** for open topic acceptance, invalid ID, duplicate ID, direct/reported/inference attribution, verbatim quote and overlap deduplication.
- [ ] **Step 2: Run test to verify RED** with `pytest -q tests/test_full_reduce.py`.
- [ ] **Step 3: Implement the two schemas, prompts and minimal deterministic validators.**
- [ ] **Step 4: Run test to verify GREEN** with `pytest -q tests/test_full_reduce.py`.
- [ ] **Step 5: Commit** `test/feat: validate full map evidence` with named files only.

### Task 2: 一次修复的 Full Map 与失败片状态

**Files:**
- Modify: `echotrace/llm/client.py`
- Modify: `echotrace/distill/full_reduce.py`
- Modify: `tests/test_full_reduce.py`

**Interfaces:**
- Consumes: `validate_findings` and existing LLM request construction.
- Produces: `map_chunk(...)` with one initial call, at most one repair call, durable success/failure checkpoint data.

- [ ] **Step 1: Write failing async tests** proving one repair succeeds, a second invalid response stops, and a completed checkpoint is reused.
- [ ] **Step 2: Run the named tests and verify RED.**
- [ ] **Step 3: Add a bounded-call option or dedicated helper without changing legacy stage retry semantics.**
- [ ] **Step 4: Run `pytest -q tests/test_full_reduce.py` and verify GREEN.**
- [ ] **Step 5: Commit** only Task 2 files.

### Task 3: 受限 Reduce 规划与执行

**Files:**
- Modify: `echotrace/distill/full_reduce.py`
- Modify: `tests/test_full_reduce.py`

**Interfaces:**
- Consumes: validated/deduplicated Map findings and complete-request budget calculation.
- Produces: `reduce_findings(...)` returning output plus `reduce_layers` of 0 or 1; raises `REDUCE_CONTEXT_TOO_LARGE` before a second intermediate layer.

- [ ] **Step 1: Write failing tests** for direct Reduce, one intermediate layer, deterministic grouping and second-layer rejection.
- [ ] **Step 2: Run the named tests and verify RED.**
- [ ] **Step 3: Implement maximal deterministic grouping using complete request admission.**
- [ ] **Step 4: Run `pytest -q tests/test_full_reduce.py` and verify GREEN.**
- [ ] **Step 5: Commit** only Task 3 files.

### Task 4: Full Pipeline 接入和 Manifest 门禁

**Files:**
- Modify: `echotrace/distill/pipeline.py`
- Modify: `tests/test_full_reduce.py`
- Modify: `tests/test_full_chunking.py`

**Interfaces:**
- Consumes: `map_chunk` and `reduce_findings`.
- Produces: full-only Phase 4 artifacts and manifest fields `map_total`, `map_succeeded`, `failed_chunks`, `reduce_layers`, `evidence_validated`, `phase4_complete`.

- [ ] **Step 1: Write failing integration tests** for full routing, incomplete manifest on failed chunk, resume reuse and sampled legacy routing.
- [ ] **Step 2: Run the named tests and verify RED.**
- [ ] **Step 3: Route only full through Phase 4 and stop publication whenever `phase4_complete` is false.**
- [ ] **Step 4: Run `pytest -q tests/test_full_reduce.py tests/test_full_chunking.py` and verify GREEN.**
- [ ] **Step 5: Commit** only Task 4 files.

### Task 5: 回归、说明和阶段收尾

**Files:**
- Modify: `docs/进度.md`
- Modify: `README.md` if user-facing mode behavior needs clarification.

**Interfaces:**
- Consumes: all Phase 4 behavior.
- Produces: reproducible verification record and an honest pending-Phase-5 boundary.

- [ ] **Step 1: Run focused extraction, recovery, output-limit and Phase 4 tests.**
- [ ] **Step 2: Run full `pytest -q` and frontend build; record exact results.**
- [ ] **Step 3: Update progress docs with verified and unverified items.**
- [ ] **Step 4: Re-run documentation-relevant smoke tests.**
- [ ] **Step 5: Commit** the documentation and any test-only correction.
