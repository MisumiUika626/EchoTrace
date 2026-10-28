# Phase 7 Mock Closure Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 用虚构 Fixture 和模拟 provider/QCE 建立可重复的 EchoTrace Mock 闭环验收，同时明确保留被跳过的 Phase 6 正式 Reply 缺口。

**Architecture:** 复用现有 `FakeModel`、`httpx.MockTransport`、生产 API、任务系统与 CLI，不新增测试专用业务开关。新增闭环测试串起已有组件，仅在 RED 揭示生产缺口时做最小修复。

**Tech Stack:** Python 3.12、pytest、FastAPI/httpx、SQLite、React/TypeScript/Vite

**Spec:** `docs/superpowers/specs/2026-10-28-mock-closure-design.md`

## Global Constraints

- 只做 Phase 7；不补做 Phase 6 正式 Reply，也不进入 Phase 8 真实外发或真实 QCE。
- 全部聊天必须来自虚构 Fixture；不读取真实密钥，不连接外部 provider/QCE。
- 每项产品改动必须先有失败测试，确认 RED 后最小实现并确认 GREEN。
- 不删除现有 Web/API、SQLite、RAG、版本、任务或扩展导入能力。

## Review Focus

- Whole 与 Chunked 两条路径都必须真正完成 full，而不是只检查规划结果。
- 模拟 QCE 返回 partial 时不得被当作完整导入成功。
- dry-run 不得读取密钥、调用 provider、输出正文或创建 Skill。
- Mock provider 只能依据本次请求生成结果，不得读取额外会话文件。
- Phase 6 旧求助接口通过不得被描述为 v13 正式 Reply 已完成。

---

### Task 1: 固化 Phase 7 基线与闭环契约

**Files:**
- Create: `tests/test_mock_closure.py`
- Modify: `tests/conftest.py`

**Interfaces:**
- Consumes: `FakeModel`, `create_app`, `import_files`, `/api/contacts/{cid}/distill/dry-run`
- Produces: 可复用的虚构闭环断言与严格 request-local Mock 记录

- [ ] **Step 1: 写失败测试**：新增 `test_mock_provider_is_request_local_and_dry_run_is_zero_io`，断言 dry-run 零 provider 调用、零 Skill 写入、响应无正文/密钥，并检查 FakeModel 输出只依赖本次 request context。
- [ ] **Step 2: 运行 RED**：`.venv/bin/pytest tests/test_mock_closure.py -q`，记录缺失闭环测试或暴露的产品缺口。
- [ ] **Step 3: 最小实现**：仅修改 `tests/conftest.py` 的 Mock 可观测性，或修复被测试证明违反零 I/O 契约的生产代码；不新增业务开关。
- [ ] **Step 4: 运行 GREEN**：`.venv/bin/pytest tests/test_mock_closure.py -q`。
- [ ] **Step 5: 提交**：`git add tests/test_mock_closure.py tests/conftest.py` 及实际最小修复文件；提交 `test: establish offline mock closure`。

### Task 2: 验收 full Whole 与 Chunked canonical 产物

**Files:**
- Modify: `tests/test_mock_closure.py`
- Modify only if RED: `echotrace/distill/*`

**Interfaces:**
- Consumes: Phase 3–5 full pipeline、`distill_manifest.json`、canonical `skill/`
- Produces: `assert_complete_full(root, expected_mode)` 测试辅助断言

- [ ] **Step 1: 写失败测试**：用同一虚构输入和两个显式容量分别完成 Whole、Chunked full；断言 manifest `complete=true`、首次覆盖完整、无失败片、Evidence/Nuwa 门禁通过、Skill 文件齐全。
- [ ] **Step 2: 运行 RED**：只运行两个新增测试并保存失败证据。
- [ ] **Step 3: 最小实现**：若真实生产路径有缺口，只修复相应 planner/pipeline/canonical gate；不得放宽 complete 条件。
- [ ] **Step 4: 运行 GREEN**：`.venv/bin/pytest tests/test_mock_closure.py -q`。
- [ ] **Step 5: 提交**：提交 `test: cover whole and chunked mock completion` 或对应 `fix:`。

### Task 3: 验收模拟 QCE 到统一导入和 Web/API/CLI 边界

**Files:**
- Modify: `tests/test_mock_closure.py`
- Modify: `tests/test_cli.py`
- Modify only if RED: `echotrace/api/routes.py`, `echotrace/cli.py`, `echotrace/ingest/qce_client.py`

**Interfaces:**
- Consumes: 模拟 QCE export job、`/api/jobs/{id}/import`、本机 API CLI
- Produces: 从模拟导出文件到统一导入、dry-run 与 full CLI 的验收证据

- [ ] **Step 1: 写失败测试**：串起模拟 QCE complete→文件落地→统一导入，并确认 partial 不进入完整导入；CLI dry-run/full 使用明确退出码且不打印 traceback/正文/密钥。
- [ ] **Step 2: 运行 RED**：`.venv/bin/pytest tests/test_mock_closure.py tests/test_cli.py -q`。
- [ ] **Step 3: 最小实现**：只修复真实衔接问题，复用现有 API/CLI，不复制导入逻辑。
- [ ] **Step 4: 运行 GREEN**：重跑上述定向集合。
- [ ] **Step 5: 提交**：提交 `test: close simulated qce and cli workflow` 或对应 `fix:`。

### Task 4: 回归、文档和阶段交付

**Files:**
- Modify: `docs/进度.md`
- Modify: `README.md`（仅在使用方式发生变化时）

**Interfaces:**
- Consumes: Tasks 1–3 的实际测试结果
- Produces: 区分已验证、Phase 6 跳过项和 Phase 8 待验项的交付记录

- [ ] **Step 1: 运行完整 Python 套件**：`.venv/bin/pytest -q`，必须全部通过。
- [ ] **Step 2: 运行前端生产构建**：在 `web/` 执行 `npm run build`。
- [ ] **Step 3: 检查差异**：`git diff --check`，并审查不含密钥、真实聊天、数据库、日志或真实响应。
- [ ] **Step 4: 更新进度**：记录准确命令、通过数、警告、Mock 范围，以及 Phase 6/8 未完成项。
- [ ] **Step 5: 提交**：`git add docs/进度.md README.md`（只添加实际修改文件）；提交 `docs: record phase seven verification`。
