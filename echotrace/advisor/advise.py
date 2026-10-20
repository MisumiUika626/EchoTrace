import json
import re

from echotrace.distill.persona import active_dir, artifact
from echotrace.distill.pipeline import evidence_ids, summary_messages
from echotrace.llm.client import estimate_tokens
from echotrace.rag.retriever import search
from echotrace.util import AppError, dumps, now, read_json

HARM = re.compile(r"(?:教我|帮我|如何|怎么|设计|话术).{0,18}(?:操控|控制[他她]|PUA|pua|情绪勒索|骗[他她]|跟踪|伪造身份|贬低[他她])|(?:操控|PUA|情绪勒索|骗[他她]).{0,12}(?:话术|技巧|方法)", re.I)
CRISIS = re.compile(r"(?:我要|我想|准备|不如|计划).{0,8}(?:自杀|结束生命|跳楼|割腕)|我不想活了")


def guard(text):
    if CRISIS.search(text):
        raise AppError("SUPPORT_NEEDED", "听起来你现在非常难受。请先联系一个信任的人陪着你，远离可能伤害自己的物品；若有即时危险，请联系当地急救或拨打120/110，也可联系12356心理援助热线。此时优先照顾你的安全。")
    if HARM.search(text):
        raise AppError("UNSAFE_REQUEST", "我可以帮你表达真实感受和边界，但不能提供操控、欺骗、跟踪或情绪勒索的话术。")


def trim_text(text, budget):
    if estimate_tokens(text) <= budget:
        return text
    while text and estimate_tokens(text) > budget:
        text = text[:int(len(text) * .85)]
    return text + "\n[内容按预算截断]"


class Advisor:
    def __init__(self, db, config, llm):
        self.db, self.config, self.llm = db, config, llm

    def context(self, cid, payload):
        if not payload.get("consent"):
            raise AppError("CONSENT_REQUIRED", "本次操作会将上下文发送到设置中的模型服务，请确认后继续。")
        draft = payload.get("draft", "").strip()
        pasted = payload.get("recent_text", "").strip()
        guard(draft + "\n" + pasted)
        if len(draft) > 8000 or len(pasted) > 20000:
            raise AppError("CONTEXT_TOO_LARGE", "请将草稿缩短到8000字以内，粘贴对话缩短到20000字以内。")
        settings = self.config.get()
        root = active_dir(self.db, cid)
        summary_only = settings["privacy"]["summary_only"] or not settings["privacy"]["allow_raw"]
        if summary_only and not read_json(root / "metadata.json", {}).get("summary_only"):
            raise AppError("SUMMARY_VERSION_REQUIRED", "当前档案基于原文生成。请先用脱敏摘要模式重新蒸馏，以免原档案中的引用被外发。")
        n = max(20, min(200, int(payload.get("recent_n", settings["advisor"]["recent_n"]))))
        rows = self.db.rows("SELECT m.id,m.ts,m.sender,m.kind,m.text FROM messages m JOIN sessions s ON s.id=m.session_id WHERE s.contact_id=? ORDER BY m.ts DESC,m.id DESC LIMIT ?", (cid, n))[::-1]
        if not rows:
            raise AppError("NOT_ENOUGH_DATA", "尚无可用于求助的聊天记录。")
        persona = artifact(self.db, cid, "persona.json")
        radar = artifact(self.db, cid, "radar.json")
        style = artifact(self.db, cid, "self_style.json")
        skill = trim_text((root / "skill" / "SKILL.md").read_text(encoding="utf-8"), 3000)
        snippets = search(self.db, self.config, cid, draft or pasted or rows[-1]["text"], 8) if not summary_only else []
        rag = []
        used = 0
        for snippet in snippets:
            item = {k: snippet[k] for k in ("text", "evidence", "ts", "context")}
            cost = estimate_tokens(item)
            if used + cost > 4000:
                break
            rag.append(item)
            used += cost
        recent = summary_messages(rows) if summary_only else rows
        compact_radar = [{k: d[k] for k in ("key", "score", "trend", "confidence", "evidence")} for d in radar["dimensions"]]
        context = {"recent_messages": recent[-30:], "earlier_summary": [{"id": m["id"], "sender": m["sender"], "text": m["text"][:120]} for m in recent[:-30]],
                   "skill": skill, "persona": persona, "radar": compact_radar, "self_style": style, "rag": rag,
                   "draft": draft, "pasted_conversation": pasted, "summary_only": summary_only}
        if summary_only and (draft or pasted):
            raise AppError("SUMMARY_MODE_INPUT", "脱敏摘要模式不外发输入的原文草稿。请清空草稿，或在设置中明确允许原文外发。")
        budget = max(6000, int(settings["advisor"]["token_budget"]))
        # Keep the newest conversation first; discard lower-priority material before recent turns.
        context["persona"] = {"sections": [{"key": s["key"], "items": s["items"][:3]} for s in persona["sections"]]}
        while estimate_tokens(context) > budget and context["earlier_summary"]:
            context["earlier_summary"].pop(0)
        while estimate_tokens(context) > budget and context["rag"]:
            context["rag"].pop()
        if estimate_tokens(context) > budget:
            context["persona"] = {}
            context["skill"] = trim_text(context["skill"], 1500)
        if estimate_tokens(context) > budget:
            raise AppError("CONTEXT_TOO_LARGE", "最近30条原文和草稿超出上下文预算。请缩短草稿或提高设置中的预算。")
        allowed = evidence_ids(context) | {m["id"] for m in context["recent_messages"]} | {m["id"] for m in context["earlier_summary"]}
        for item in context["rag"]:
            allowed.update(m["id"] for m in item["context"])
        return context, allowed

    async def advise(self, cid, payload):
        context, allowed = self.context(cid, payload)
        if payload.get("regenerate_strategy"):
            context["regenerate_strategy"] = payload["regenerate_strategy"]
            context["variation"] = "为指定策略提出一个不同的新版本；其余策略仍按 schema 输出。"
        output = await self.llm.call("advise_top3", context, allowed, override={"temperature": .8})
        for option in output["options"]:
            guard("帮我" + option["text"])
        output["context_used"] = {"messages": len(context["recent_messages"]) + len(context["earlier_summary"]),
                                  "quotes": len(context["rag"]), "skill": bool(context["skill"]), "radar": bool(context["radar"]),
                                  "estimated_tokens": estimate_tokens(context)}
        if len(context["rag"]) < 2:
            output["confidence_note"] = "检索命中少于2条，历史依据有限；优先参考稳妥型建议。" + output["confidence_note"]
        self.db.execute("INSERT INTO local_actions(contact_id,action,payload,created_at) VALUES(?,?,?,?)", (cid, "advice", dumps(output), now()))
        return output

    async def chat(self, cid, payload):
        context, allowed = self.context(cid, payload)
        history = [] if context["summary_only"] else self.db.rows("SELECT role,content FROM conversations WHERE contact_id=? ORDER BY id DESC LIMIT 20", (cid,))[::-1]
        context["simulation_history"] = history
        output = await self.llm.call("chat_roleplay", context, allowed, override={"temperature": .7})
        with self.db.connect() as conn:
            for role, content in (("user", payload.get("draft", "")), ("assistant", output["text"])):
                conn.execute("INSERT INTO conversations(contact_id,role,content,created_at) VALUES(?,?,?,?)", (cid, role, content, now()))
        return output
