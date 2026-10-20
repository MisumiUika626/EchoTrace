# EchoTrace · 回声痕迹

本机运行的 QQ 私聊归档、人物蒸馏与对话求助工作台。FastAPI / SQLite / React / TypeScript / ECharts，中文界面，Windows 优先。

支持「导入 → 数据体检 → 消息浏览 → 蒸馏四件套 → 证据回查 → 对话求助 → 追加与版本回滚」。QCE 是可选的独立导出工具；本地文件流程不依赖它。

## 安装与启动

需要 Python 3.11+、Node.js 22+ 和 npm。以下在项目根目录用 PowerShell 执行；安装和构建依赖时需要网络：

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe scripts/build.py
.\.venv\Scripts\echotrace.exe
```

启动后自动创建本地数据目录并打开 <http://127.0.0.1:8765>。完成安装与构建后，日常只需最后一条命令；已激活虚拟环境时可直接运行 `echotrace`。不需要账号，也不需要先配置模型。

```powershell
# 更换端口、数据目录，或关闭自动打开浏览器
.\.venv\Scripts\echotrace.exe serve --port 8766 --data-dir "D:\EchoTraceData" --no-browser
```

默认数据目录是**启动时所在目录**下的 `data/`，请固定启动目录或始终指定 `--data-dir`。服务仅监听 `127.0.0.1`。API 文档：<http://127.0.0.1:8765/api/docs>。在启动终端按 Ctrl+C 停止。

## 先体验虚构样例

仓库自带 5,000 条完全虚构的消息，包含图片占位、表情、约定、冲突与和解。

1. 点击「导入聊天记录」，选择 `tests/fixtures/fictional.jsonl`，解析预览。
2. 在身份确认中勾选 `fiction-me` 为自己，人物称呼填「林间（虚构）」，对方标识填 `fiction-them`。
3. 导入后可离线查看体检报告、搜索消息、收藏原话。同一人物再次导入同一文件不会增加消息。
4. 如需蒸馏，先配置你自己的模型服务，再明确同意本次上下文外发。少于 200 条消息会拒绝蒸馏。

也可以在启动前用命令行导入：

```powershell
.\.venv\Scripts\echotrace.exe import --file tests/fixtures/fictional.jsonl --name "林间（虚构）" --uid fiction-them --me fiction-me
.\.venv\Scripts\echotrace.exe
```

导入支持 JSON / JSONL / TXT / HTML / XLSX，多文件、粘贴文本和自定义正则。大文件推荐逐行 JSONL；旧 `.xls` 请先转换为 `.xlsx`。HTML 兼容常见结构，并非任意网页都可解析。媒体保留原始字段和占位，不理解图片、语音内容，也不自动下载远程素材。

## 模型与隐私

在左侧栏底部「设置」中填写 OpenAI 兼容 Base URL（通常包含 `/v1`）、API Key、模型 ID、思考参数适配器，或直接打开 [模型设置](http://127.0.0.1:8765/#settings)。默认模型 ID 为 `deepseek-v4.1-flash`、思考深度 Max，这只是可修改的默认配置，**不保证你的服务商提供该模型或支持 Max**。

设置页展示适配器实际发送的参数，可选择 `reasoning_effort`、`thinking.budget_tokens`、自定义字段或不发送思考参数。连通性测试只读取模型列表，不发送聊天，不验证生成能力或思考深度是否生效。

- 导入、统计、浏览、检索均在本机进行。没有遥测、云同步或自动代发 QQ 消息。
- 主动蒸馏、求助、模拟对话才会向配置的模型服务发送相关上下文；首次配置与实际调用都有明确确认。
- 「仅匿名统计摘要」不发送聊天原文、名字或旧模拟对话；输入质量明显下降，且不自动抽取原话语料。你本次主动填写的求助草稿仍属于需发送的输入。
- 图片和语音文件不发送给模型。应用不能控制服务商对已发送文本的保存和使用方式。

所有模型输出经统一网关、结构校验、证据归属检查、重试和用量记账。验证失败的输出保存在本机 `review/` 供复核，任务失败，不发布为有效画像。用量是服务商返回值或本地估算，费用以服务商账单为准。

## 人物工作台

- **画像**：八个固定板块；区分原文支撑和模型推断，点击证据定位具体消息。
- **雷达与演化**：八维综合分固定为统计 60% + 模型 40%，显示置信度、差异说明。月度/近一年/90天/30天展示本地统计，近期范围截至该版本最后一条消息；大事记附证据，日期是证据发言日期。
- **人物 Skill**：蒸馏模型加载女娲 Skill 原文，执行六维研究、三重筛选、独立测试与精炼，再生成人物自己的 `SKILL.md`。研究、证据和验证报告随 ZIP 下载，可主动安装到用户 `~/.agents/skills/` 或启动目录下 `.claude/skills/`。已有同名 Skill 不覆盖；重跑与本地重建均创建新版本，保留历史快照。
- **重要话语**：逐字原话、主题、评分、前后各三条上下文，支持搜索、人工收藏、删除及导出。
- **现实求助**：结合人物档案、RAG、最近对话和自己的表达风格，生成稳妥/升温/真实三种策略；附证据、风险和第二轮预案，可复制、采纳、收藏或重生成其中一条。
- **模拟对话**：明确标注 AI 模拟，保存本地历史；不是与真人通信。
- **版本**：保存不可变快照、对比、回滚。未变分片可复用缓存；分片、模型、模板或角色映射变化时重新计算。
- **任务**：进度、token 用量、暂停、取消、提高预算后继续。重启后中断任务保留为暂停，由你手动恢复；不会自动继续模型外发。

关系分数是文本统计与模型推断，不是心理测评，也不能证明真人的想法。格式质检、模型语义测试与本人认可分别是不同的判断；通过测试后仍需回查原文。

默认检索使用 jieba / FTS5 + 256 维**词项哈希向量** + RRF，不宣称语义理解。可安装 `.[semantic]` 并在设置页选择已存在的 SentenceTransformer 本地目录；应用不自动下载模型。sqlite-vec 可用时使用向量扩展，否则有本地检索降级。

## 用女娲蒸馏人物 Skill

模型请求实际携带女娲的 `SKILL.md`、`extraction-framework.md` 和 `skill-template.md` 完整原文。优先读取用户本机 `~/.agents/skills/`、`~/.codex/skills/` 下的 `nuwa-skill` 或 `huashu-nuwa`；未安装时使用项目随附的 MIT 授权副本，无需另装女娲。报告记录材料来源与 SHA256。

1. 导入并确认人物身份，在「设置」配置模型；点击「开始蒸馏」并确认本次外发。可勾选在三个女娲摘要检查点暂停，默认自动继续。
2. 模型以**纯本地语料模式**研究身份与观点、对话反应、表达DNA、他者评价、决策行动和时间线。仅使用该版本选定的聊天样本，不搜索私人信息。
3. 候选心智模型经过跨领域、生成力、排他性三重检验，证据不足降为启发式或舍弃。独立测试设计者出题，扮演请求只收到人物 Skill 与问题，评审请求再对照原文；最多修复两轮，然后双视角精炼并复验。
4. 在人物「Skill」页查看研究、筛选、实际问答、薄弱项与精炼结果。下载 ZIP 或安装后，用「用某某的视角」等触发词启用生成的人物 Skill。

旧人物档案在「Skill」页点击**「模型重跑 Skill」**即可补齐流程；复用原有画像和采样输入，生成新版本。「本地重建为新版本」不调用模型，不能补做研究或验证；重建内容改变时会标记原验证失效。

女娲部分基础为 12 次模型请求；有建议整合与复验时为 15 次，每轮修复另加 3 次，最多两轮，另有前置分片分析及失败重试成本。每次请求包含方法原文，建议结合预算提示选择模型。已完成阶段有检查点，中断后手动继续只补做缺失部分。仅匿名统计模式缺少真实观点和语气，会明确降级，不能据统计补造人物。

生成的人物 Skill 包含角色规则、心智模型、决策启发式、表达DNA、回答工作流与诚实边界；`references/` 同时保存六维研究、原话、所用证据和女娲报告。导出包包含聊天证据，请按原始记录的隐私范围保存与分享。本轮已验证流程与输入隔离，真实模型的人物还原质量仍需人工核对。

## 可选：连接 QCE 导出 QQ 私聊

本机尚未安装 QCE；EchoTrace 不会代装、启动或打包它。请从导出中心的官方 [qq-chat-exporter Releases](https://github.com/shuakami/qq-chat-exporter/releases) 入口，按照上游文档自行准备工具和登录环境。

回填本机地址（默认 `http://127.0.0.1:40653`）与 Token 并检测；连接后可选会话或查询 QQ 号对应 UID，设置时间、格式、资源链接和目录，导出后自动导入。支持 QCE 任务刷新、取消和删除，WebSocket 失败时轮询兜底，以及你主动开启的定时归档。该定时任务只连接本机 QCE，不触发模型蒸馏。

漫游导出仅支持私聊；独立查看模式不支持此导出。`partial` 会提示可能缺失，任何结果都不能证明腾讯服务端记录完整。ZIP 只用于归档，导入前请手动解压。当前 QCE 协议通过模拟测试，**尚未连接真实 QCE 验证版本兼容性**。

## 命令行任务

`export` / `distill` 连接已启动的 EchoTrace 服务，并持续显示任务进度。它们的 `--port` 必须与服务一致，数据目录由服务决定。`--uid` 在导入时表示对方稳定标识，在导出时表示 QQ 号；导出也可使用 `--peer-uid`。

```powershell
# 在另一个终端启动服务并配置好模型后，明确授权本次蒸馏
.\.venv\Scripts\echotrace.exe distill --contact 1 --consent --tier standard --token-limit 500000

# 仅发送匿名统计摘要；模型和思考深度可按次覆盖
.\.venv\Scripts\echotrace.exe distill --contact 1 --consent --summary --model your-model --thinking high

# 起止时间为 Unix 秒。需要本机 QCE 已就绪
.\.venv\Scripts\echotrace.exe export --name "示例人物" --uid 123456 --me 654321 --start 1767225600 --end 1769904000 --format STREAMING_JSONL

# 更多选项
.\.venv\Scripts\echotrace.exe --help
```

导出还可用 `--output-dir`、`--no-auto-import`。Ctrl+C 退出命令行进度查看时，已创建的后台任务仍保留，可在任务中心处理。

## 本地数据与备份

```text
data/
  echotrace.db                  SQLite WAL、消息、检索、设置、任务、用量
  .secrets.json                 模型密钥 / QCE Token（独立文件）
  people/{slug}/
    health.json                体检
    persona.json  radar.json   当前画像 / 雷达
    quotes.jsonl  metrics.json self_style.json
    skill/                     SKILL.md + references/（研究、原话、证据、验证报告）
    nuwa_report.json           女娲调研、筛选、验证与精炼结果
    rag/                       索引配置；向量数据在 SQLite
    versions/v{n}/             四件套及参数、来源、检查点快照
  exports/                     上传与导出文件
  prompts/                     自定义提示词及历史
  review/                      未通过校验的模型输出
```

聊天与密钥均**未加密存储**，Windows 的权限设置不等同于独立的保险库。建议停止服务后复制整个数据目录备份，包含 `.secrets.json` 时按密钥管理；不要只复制正在写入的 SQLite 主文件。`data/`、数据库、构建产物、虚拟环境均已排除 Git。自定义数据目录也请自行排除版本管理和云同步。

## 验证与开发

开发与验收仅使用虚构记录和模拟模型，未向真实模型发送记录，也未安装 QCE。

```powershell
# 自动化测试；禁用宿主机无关 pytest 插件
$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD='1'
$env:TEMP="$pwd\web\.build-tmp"
$env:TMP=$env:TEMP
New-Item -ItemType Directory -Force $env:TEMP | Out-Null
.\.venv\Scripts\python.exe -m pytest -p pytest_asyncio.plugin -q

# 类型检查、生产构建和静态资源打包
.\.venv\Scripts\python.exe scripts/build.py

# 可选浏览器验收：需系统 Microsoft Edge，不下载浏览器
.\.venv\Scripts\python.exe -m pip install playwright
.\.venv\Scripts\python.exe scripts/verify_browser.py

# 真实子进程强杀/手动恢复，以及空数据目录 CLI 启动验证（均不连接真实模型/QCE）
.\.venv\Scripts\python.exe scripts/verify_recovery.py
.\.venv\Scripts\python.exe scripts/verify_startup.py

# 生成虚构文件并测量解析、清洗、插入、FTS、统计的耗时和进程峰值 RSS
.\.venv\Scripts\python.exe scripts/benchmark.py --count 10000
.\.venv\Scripts\python.exe scripts/benchmark.py --count 1000000
```

Windows 11 / Python 3.14 本机实测：36 项测试通过；本轮 Edge 完整流程包含女娲报告和 390px 移动布局。历史强杀恢复、空目录 CLI 启动及性能结果仍保留：10,000 条导入 0.797 秒 / 66.93 MB，1,000,000 条导入 304.108 秒 / 81.21 MB。这些是特定机器上的虚构 JSONL 结果，未包含真实模型蒸馏耗时。详细范围、原始结果和待验证项见 [进度与验收](docs/进度.md)。

前端开发可在 `web/` 执行 `npm run dev`；后端通过 `ECHOTRACE_DEV=1` 允许本机 5173 端口的开发来源。生产运行无需 Node.js 常驻。

## 当前限制

- 真实模型的生成质量、20 条事实人工抽查、真实 QCE、干净机器安装与系统级抓包尚未验收。
- 模拟对话的 SSE 在上游结构化结果校验完成后分段发送，尚非原生上游 token 流。
- 深度档按全部记录分片，快速/标准档确定性抽样；追加或重采样可能使部分旧分片重新计算，并非最少重算。
- HTML 解析会整体读取文件；大体量请使用 JSONL。媒体目前只归档元数据与占位。
- 结构持续无效时保存待复核文件并失败，不会自动把宽松解析产物发布为可信画像。
- 依赖 jieba / Uvicorn 的部分接口有弃用警告；目前测试通过，已限制 setuptools 版本以兼容 jieba 的运行时依赖。

## 来源与许可证

[完整需求](docs/EchoTrace-项目提示词.md) · [实现计划](docs/实现计划.md) · [续接提示词](docs/快速启动提示词.md) · [NOTICE](NOTICE.md)

[qq-chat-exporter](https://github.com/shuakami/qq-chat-exporter) 为 GPL-3.0，本项目只通过 HTTP API 和导出格式互操作，不复制或分发其源码、二进制。女娲 Skill 原文按 MIT 授权随附，具体来源及 NapCat 相关出处见 NOTICE。EchoTrace 自身许可证由项目所有者决定，目前未指定。
