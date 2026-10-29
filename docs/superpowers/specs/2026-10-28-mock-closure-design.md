# Phase 7 Mock 闭环设计

## 意图

用完全虚构的数据和确定性模拟 provider，把现有分散测试收束为可重复的课程闭环验收；发现缺口时只做最小修复，不连接真实 QQ、QCE 或模型服务。

## 范围

- 使用 `tests/fixtures/fictional.jsonl` 和现有 `FakeModel`，验收本地文件导入、full Whole/Chunked、Evidence、canonical Skill、正式 manifest、Web/API、CLI dry-run 与模拟 QCE 导出后统一导入。
- 验证 Mock 不读取额外会话文件、不联网、不使用真实密钥，错误注入继续通过替换 transport 响应完成。
- 保留现有 FastAPI、React、SQLite、RAG、版本与任务能力，不做无关重构。

## 已知边界

- 用户决定 Phase 6 不改三风格与现有界面，沿用当前 `/advisor` 接口。因此 Phase 7 只回归旧 `safe/warm/honest` 求助接口可接通，不宣称 v13 的正式三风格 Reply、complete/指纹/Reply 预算门禁或 CLI Reply 已验收。
- Mock 通过不代表真实模型质量、Evidence 语义支持关系或真实 QCE 兼容性；这些仍属于 Phase 8。

## 方案比较

1. **闭环验收层（采用）**：新增少量端到端测试，复用现有 Fixture、FakeModel、API 与 CLI；只有测试暴露真实缺口时才改产品代码。优点是低风险、能证明组件确实衔接，缺点是不会补回被跳过的 Phase 6 正式契约。
2. **重写专用 Mock 框架**：独立实现一套 Phase 7 模拟服务。会重复现有 `FakeModel`/`MockTransport`，且容易形成测试专用业务开关，不采用。
3. **仅汇总现有测试**：不新增闭环测试，只写报告。不能证明跨组件衔接，不采用。

## 验收

- 同一套虚构输入分别触发 Whole 与 Chunked full，并得到 complete canonical Skill、可回溯 Evidence 和 manifest。
- 模拟 QCE 成功结果进入统一导入，失败/partial/轮询/取消继续由现有回归覆盖。
- dry-run 在 provider 配置和密钥读取前完成，零联网、零 Skill 写入、无正文输出。
- Web/API 与必要 CLI 走真实本机服务边界，返回明确退出码。
- 完整 Python 测试、前端生产构建与 `git diff --check` 通过；报告中分开列出已验证与 Phase 6/8 未验证事项。
