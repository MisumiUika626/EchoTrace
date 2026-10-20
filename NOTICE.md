# 第三方来源说明

EchoTrace 自主实现本地工作台，第三方来源及使用方式如下：

- [qq-chat-exporter](https://github.com/shuakami/qq-chat-exporter)：GPL-3.0。通过用户独立安装的服务的 HTTP API 与导出文件互操作；不复制、链接或分发其源码与二进制。
- [女娲 · Skill 造人术](https://github.com/alchaincyf/nuwa-skill)，作者花叔，MIT License。蒸馏模型实际加载 `SKILL.md`、`extraction-framework.md`、`skill-template.md`，在纯本地语料模式下提炼人物 Skill；应用负责分阶段调用与落盘。优先读取本机安装版本，否则使用 `echotrace/templates/nuwa/` 中的原文副本，该目录附有完整上游许可证。可调用本机已有的 quality_check.py 作格式检查。人物内容来源为用户提供的聊天，不声称由上游作者创作或背书。随附副本未由 EchoTrace 自动联网更新。
- [NapCatQQ](https://github.com/NapNeko/NapCatQQ)：QQ 导出生态相关项目；EchoTrace 不分发、不自动启动、不调用发送消息接口。

Python 与 npm 依赖按各自许可证使用，完整列表见 pyproject.toml 与 web/package-lock.json。EchoTrace 自身授权条款待项目所有者决定。
