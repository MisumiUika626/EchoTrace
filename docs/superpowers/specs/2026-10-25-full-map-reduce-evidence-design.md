# Full Map-Reduce 与 Evidence 设计

## 范围

本阶段只实现 v13 Phase 4：开放主题 Map、一次 JSON 修复、至多一层中间 Reduce、Evidence 机械验证，以及失败片和不完整状态记录。`sampled` 保持旧流程；canonical Skill、正式回复和 `complete=true` 留给后续阶段。

## 兼容结构

`full` 继续复用 Phase 3 的动态分片和持久检查点。每个输入片先生成开放主题观察，再由独立聚合器去重并计算完整 Reduce 请求；能一次容纳就直接 Reduce，不能容纳则先分成若干中间组，各组 Reduce 一次，再做一次最终 Reduce。最终请求仍超容量时明确失败，不递归增加层数。

`sampled` 继续使用既有六维抽取、EvidenceIndex 选取和画像聚合，避免本阶段改变预览成本、输出或恢复语义。

## Map 合同

- 新增仅供 full 使用的 `full_map` schema 和 Prompt；主题为自由字符串，不预设人格维度。
- 每条观察保存 `value`、`evidence`、`source_kind`、`inferred`、`confidence`、`note`，直接引语另存 `quote`。
- `source_kind` 区分目标本人直接表达、另一位发言人的转述和推断。
- LLM 返回的结构或 Evidence 不合法时，只允许一次面向原输出的 JSON 修复；第二次仍失败，该片记为失败。
- 已成功片和失败片均有稳定检查点，恢复时不重复调用成功片。

## Evidence 机械验证

Map 与 Reduce 的每条实质结论必须满足：Evidence ID 存在、属于当前授权范围、没有重复 ID；`direct` 至少含目标本人消息；只由本人消息支持的对方结论必须是 `reported` 或 `inference`；`inference` 必须明确标记并带说明；quote 必须逐字来自目标本人消息。机械验证不宣称语义支持关系已被证明。

重叠消息可供上下文使用，但每片输出按其首次覆盖 ID 归属；同一 Evidence 组合和规范化结论在聚合时去重，不能因出现在重叠片而重复计票。

## Reduce 合同

- 新增 `full_reduce` schema 和 Prompt，输出开放主题 findings、contradictions、unknowns。
- Reduce 保留稳定模式、少数重要观察、人物特有信息、冲突、变化与未知，不按 Evidence 数量投票。
- 聚合前执行确定性去重；Reduce 输出再次执行范围、角色和推断标记验证。
- 只允许直接 Reduce，或“一层中间 Reduce + 最终 Reduce”。最终 Reduce 仍超容量时抛出 `REDUCE_CONTEXT_TOO_LARGE`。

## 状态与失败

`manifest.json` 增加 Phase 4 状态：Map 总数、成功数、失败片、Reduce 层数、Evidence 验证结果和 `phase4_complete`。任一片失败、Evidence 校验失败或 Reduce 未完成时，`phase4_complete=false`，并阻止 full 继续发布旧式完整画像；但已有输入和成功检查点保留以供恢复。

本阶段继续保持 `metadata.complete=false`，不生成正式 `distill_manifest.json`，不宣称 canonical Skill 已完成。

## 验收

自动化测试覆盖开放主题、一次修复上限、成功片恢复、失败片状态、Evidence 范围与角色、重叠去重、直接 Reduce、一层中间 Reduce、禁止第二层，以及 sampled 回归。所有测试使用离线模拟模型。
