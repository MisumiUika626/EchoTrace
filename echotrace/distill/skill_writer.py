import os
import re
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from echotrace.ingest.parsers import TZ
from echotrace.util import AppError, atomic_write, write_json

FILES = ["01-persona.md", "02-conversations.md", "03-expression-dna.md", "04-external-views.md", "05-decisions.md", "06-timeline.md"]


def citation(item):
    return "evidence: " + ", ".join(str(x) for x in item["evidence"])


def describe(item):
    tag = "推断" if item["inferred"] else "原文支撑"
    return f"- {item['value']}（{tag}，置信度 {item['confidence']:.0%}；{citation(item)}）" + (f"\n  依据：{item['note']}" if item.get("note") else "")


def write_skill(target, person, persona, skill, observations, stats, quotes):
    root = target / "skill"
    research = root / "references" / "research"
    research.mkdir(parents=True, exist_ok=True)
    sections = {s["key"]: s for s in persona["sections"]}
    name, slug = person["display_name"].replace("\n", " "), person["slug"]
    start = datetime.fromtimestamp(stats["first_ts"], TZ).strftime("%Y-%m-%d")
    end = datetime.fromtimestamp(stats["last_ts"], TZ).strftime("%Y-%m-%d")
    lines = ["---", f"name: {slug}-perspective", "description: |", f"  {name}的聊天表达与思维框架。基于{stats['total']}条聊天记录。",
             f"  当用户说「用{name}的视角」「{name}会怎么看」「切换到{name}」时使用。", "---", "", f"# {name} · 思维操作系统", "",
             "## 角色扮演规则（最重要）", "首次明确说明是基于聊天记录的 AI 模拟，不是真人。用第一人称与记录内的语气回应；不得编造共同回忆，不代真人承诺。",
             "未知观点标明推断。用户说「退出」「切回正常」时结束角色扮演。", "", "## 身份卡"]
    lines += [describe(x) for x in sections["identity"]["items"]] or ["- 身份资料不足，不补写现实身份。"]
    lines += ["", "## 核心心智模型"]
    for i, model in enumerate(skill["models"], 1):
        lines += [f"### 模型{i}: {model['name']}", describe(model), f"**应用**：{model['application']}", f"**局限**：{model['limitation']}"]
    lines += ["", "## 决策启发式"]
    lines += [f"{i}. {describe(item)[2:]}" for i, item in enumerate(skill["heuristics"], 1)]
    lines += ["", "## 表达DNA", "句式、词汇、语气与节奏只能遵循以下记录证据；没有证据的口头禅不添加。"]
    lines += [describe(x) for x in sections["language_dna"]["items"]]
    lines += ["", "## 人物时间线（关键节点）"] + [describe(x) for x in sections["narrative"]["items"]]
    lines += ["", "## 价值观与反模式"] + [describe(x) for x in sections["values"]["items"]]
    lines += ["", "### 观察到的张力", *([describe(x) for x in persona["tensions"]] or ["未获得足以判断张力的证据，不虚构矛盾。"]),
              "对未证实的矛盾保持开放，不将不同情境直接解释为人格冲突。", "", "## 智识谱系", "记录未覆盖的影响来源不作推断。", "", "## 诚实边界",
              f"- 本 Skill 基于 {stats['total']} 条聊天记录（{start} ~ {end}）提炼，仅反映此人在与用户对话场景中的表现，不代表其全部人格。",
              "- 模型推断不是本人观点；所有结论须回查 evidence 对应的原始消息。",
              "- 不能从聊天推断心理诊断；不能替真人作承诺、决定或编造私密事实。",
              "- 数据缺失与相互矛盾的证据仍保留；记录之外的生活不在本档案覆盖范围内。", "", "## 附录：调研来源",
              "一手来源：本地原始聊天记录，原文引用见 references/quotes.md。",
              "每条结论的原始证据保存在 EchoTrace，详见 references/research/。",
              "格式参考：女娲 · Skill 造人术 https://github.com/alchaincyf/nuwa-skill；内容由 EchoTrace 按聊天证据生成。"]
    atomic_write(root / "SKILL.md", "\n".join(lines) + "\n")
    groups = [sections["identity"]["items"] + sections["emotion"]["items"], sections["interaction"]["items"],
              sections["language_dna"]["items"], [], skill["heuristics"], sections["narrative"]["items"]]
    for filename, items in zip(FILES, groups):
        atomic_write(research / filename, "# " + filename[3:-3] + "\n\n" + ("\n".join(describe(x) for x in items) if items else "证据不足；未将聊天转述冒充独立第三方证据。") + "\n")
    atomic_write(root / "references" / "quotes.md", "# 关键原话\n\n" + "\n\n".join(f"> {q['text']}\n\n{datetime.fromtimestamp(q['ts'], TZ).date()} · evidence: {', '.join(map(str,q['evidence']))}" for q in quotes))
    result = quality_check(root / "SKILL.md")
    write_json(target / "skill_quality.json", result)
    return result


def quality_check(path):
    external = Path.home() / ".agents" / "skills" / "nuwa-skill" / "scripts" / "quality_check.py"
    if external.exists():
        result = subprocess.run([sys.executable, str(external), str(path)], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30,
                                env={**os.environ, "PYTHONIOENCODING": "utf-8"}, creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        checks = [{"name": line.strip(), "passed": "PASS" in line} for line in result.stdout.splitlines() if "PASS" in line or "FAIL" in line]
        return {"passed": result.returncode == 0 and len(checks) == 6, "checks": checks, "output": result.stdout,
                "checker": "本机女娲 quality_check.py", "external": True}
    content = path.read_text(encoding="utf-8")
    checks = [
        ("心智模型数量", 3 <= len(re.findall(r"^### 模型\d", content, re.M)) <= 7),
        ("模型局限性", "**局限**" in content), ("表达DNA", "## 表达DNA" in content),
        ("诚实边界", content.count("\n- ", content.index("## 诚实边界")) >= 3),
        ("内在张力", "张力" in content and "矛盾" in content), ("一手来源", "一手来源" in content and "evidence:" in content)]
    return {"passed": all(ok for _, ok in checks), "checks": [{"name": name, "passed": ok} for name, ok in checks],
            "checker": "EchoTrace 格式检查（本机未安装女娲检查器）", "external": False}


def install_skill(source, slug, destination, project_root=None):
    base = Path.home() / ".agents" / "skills" if destination == "agents" else Path(project_root or Path.cwd()) / ".claude" / "skills"
    target = base / f"{slug}-perspective"
    if target.exists():
        raise AppError("SKILL_EXISTS", "目标 Skill 已存在。为保护本机修改，请先手动备份或移走再安装。", 409)
    base.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, target)
    return {"path": str(target)}
