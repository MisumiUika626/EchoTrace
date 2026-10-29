"""Evidence-bounded adaptation of Nuwa's local-corpus workflow.

Each role uses an isolated model request. No online research or Codex runtime is
required by the application; completed requests are durable resume checkpoints.
"""
import asyncio
import hashlib

from echotrace.jobs import JobStopped
from echotrace.llm.client import estimate_tokens
from echotrace.llm.nuwa_skill import load_nuwa_skill
from echotrace.distill.evidence import EvidenceIndex, DIMENSION_FILTERS
from echotrace.distill.skill_writer import citation_ids
from echotrace.util import AppError, atomic_write, dumps, now, read_json, write_json

DIMENSIONS = [
    ("01-persona.md", "身份与系统观点"), ("02-conversations.md", "对话与即时反应"),
    ("03-expression-dna.md", "表达DNA"), ("04-external-views.md", "他者观察与批评"),
    ("05-decisions.md", "决策、承诺与行动"), ("06-timeline.md", "时间线与观点变化"),
]
STAGES = ("nuwa_research", "nuwa_synthesize", "nuwa_test_plan", "nuwa_respond", "nuwa_evaluate", "nuwa_refine", "nuwa_integrate")
METHOD = "echotrace-nuwa-local-v1"


def ids_in(value):
    if isinstance(value, dict):
        return set(value.get("evidence", [])) | set().union(*(ids_in(v) for k, v in value.items() if k != "evidence"))
    if isinstance(value, list):
        return set().union(*(ids_in(v) for v in value))
    return set()


def limited(items, budget):
    result, used = [], 0
    for item in items:
        cost = estimate_tokens(item)
        if used + cost <= budget:
            result.append(item)
            used += cost
    return result


def corpus_packet(target, observations, summary_only):
    """Read only this version's scoped/sample input, never unrelated DB rows."""
    manifest = read_json(target / "manifest.json")
    lookup = {}
    for filename in manifest["chunks"]:
        for message in read_json(target / "inputs" / filename):
            lookup[message["id"]] = message
    ordered = sorted(lookup.values(), key=lambda m: (m.get("sequence", 0), m["id"]))
    # Round-robin over time avoids spending the evidence allowance on one period.
    stride = max(1, len(observations) // 70)
    obs = limited(observations[::stride], 5000)
    source_ids = ids_in(obs)
    relevant = set(source_ids)
    for index, message in enumerate(ordered):
        if message["id"] in source_ids:
            relevant.update(m["id"] for m in ordered[max(0, index-1):index+2])
    selected = [m for m in ordered if m["id"] in source_ids] + [m for m in ordered if m["id"] in relevant - source_ids]
    messages = sorted(limited(selected, 6500), key=lambda m: (m.get("sequence", 0), m["id"]))
    allowed = {m["id"] for m in messages}
    obs = [x for x in obs if ids_in(x) <= allowed]
    if summary_only:
        from echotrace.distill.pipeline import summary_messages
        messages = summary_messages(messages)
    target_messages = [m for m in messages if m["sender"] in ("them", "B")]
    sizes = [len(lookup[m["id"]]["text"]) for m in target_messages]
    text = "" if summary_only else "\n".join(m["text"] for m in target_messages)
    quant = {"sample": len(sizes), "mean_characters": round(sum(sizes) / max(1, len(sizes)), 1),
             "short_message_share": round(sum(n <= 20 for n in sizes) / max(1, len(sizes)), 3),
             "question_marks": text.count("？") + text.count("?"), "exclamation_marks": text.count("！") + text.count("!"),
             "note": "仅为所选证据样本的本地统计，不能据此判断心理、价值观或词语偏好；摘要模式不统计原文标点。"}
    return {"observations": obs, "evidence_messages": messages, "expression_statistics": quant, "summary_only": summary_only}


def select_framework(synthesis, summary_only=False):
    models, heuristics, decisions = [], list(synthesis["heuristics"]), []
    for item in synthesis["candidates"]:
        scenarios = item["scenarios"]
        distinct_scenes = {x["domain"].strip().casefold() for x in scenarios}
        distinct_evidence = {tuple(sorted(set(x["evidence"]))) for x in scenarios}
        cross = len(distinct_scenes) >= 2 and len(distinct_evidence) >= 2
        flags = [cross, bool(item["generative"] and item["generative_reason"].strip()),
                 bool(item["distinctive"] and item["distinctive_reason"].strip())]
        count = sum(flags)
        action = "model" if count == 3 and not summary_only else "heuristic" if count and not summary_only else "discard"
        if action == "model" and len(models) >= 7:
            action = "heuristic"
        decisions.append({"name": item["name"], "cross_domain": flags[0], "generative": flags[1],
                          "distinctive": flags[2], "action": action, "evidence": item["evidence"],
                          "reason": f"跨域：{'通过' if flags[0] else '不足'}；生成力：{item['generative_reason']}；排他性：{item['distinctive_reason']}"})
        if action == "model":
            models.append(item)
        elif action == "heuristic":
            heuristics.append({k: item[k] for k in ("value", "evidence", "inferred", "confidence", "note")})
    seen, unique = set(), []
    for item in heuristics:
        if item["value"] not in seen:
            seen.add(item["value"])
            unique.append(item)
    return {"models": models, "heuristics": [] if summary_only else unique[:10],
            **{k: synthesis[k] for k in ("expression", "values", "tensions", "lineage", "unknowns")}, "selection": decisions}


def method_sources():
    skill = load_nuwa_skill()
    return {"name": skill["name"], "source": skill["source"], "sha256": skill["sha256"], "delivery": "system_message"}


def research_markdown(dimension, result):
    labels = {"direct": "一手原话", "reported": "聊天转述（未经独立核实）", "inference": "研究推断"}
    lines = [f"# {dimension}", "", "来源：用户提供的本地聊天样本；未作网络搜索。", "", "## 发现"]
    for key, title in (("findings", "发现"), ("contradictions", "矛盾与情境差异")):
        if key != "findings":
            lines += ["", "## " + title]
        for item in result[key]:
            lines += [f"- {item['value']}（{labels[item['source_kind']]}，置信度 {item['confidence']:.0%}；evidence: {', '.join(map(str, item['evidence']))}）", f"  依据/限制：{item['note'] or '仅代表记录中的发言，不据此证明现实行动。'}"]
        if not result[key]:
            lines += ["- 未获得足够证据。"]
    lines += ["", "## 信息缺口", *["- " + x for x in result["gaps"]]]
    return "\n".join(lines) + "\n"


def normalize_research_sources(output, messages):
    """An explicitly labelled inference must not masquerade as primary evidence.

    Only reconcile the source label when the model already supplied an
    inference flag, its limitation, and known evidence. Never turn a direct
    factual claim into an inference merely to pass the attribution check.
    """
    allowed = {m["id"] for m in messages}
    corrections = []
    for field in ("findings", "contradictions"):
        for index, item in enumerate(output[field]):
            if (item["source_kind"] == "direct" and item["inferred"] is True
                    and item["note"].strip() and item["evidence"]
                    and set(item["evidence"]) <= allowed):
                corrections.append({"field": field, "index": index,
                                    "original_source_kind": "direct", "source_kind": "inference",
                                    "evidence": list(item["evidence"]),
                                    "reason": "原条目已标明推断及限制，仅将来源类型从一手原话统一为研究推断。"})
                item["source_kind"] = "inference"
    return corrections


def validate_research_sources(output, messages, filename):
    lookup = {m["id"]: m for m in messages}
    for item in output["findings"] + output["contradictions"]:
        if not set(item["evidence"]) <= lookup.keys():
            raise ValueError("研究证据必须来自本维度提供的原始消息")
        if item["source_kind"] == "direct" and any(lookup[mid]["sender"] not in ("them", "B") for mid in item["evidence"]):
            raise ValueError("一手观点只能引用目标本人发言；他人评价须标 reported")
        if filename == "04-external-views.md" and item["source_kind"] == "direct":
            raise ValueError("聊天里的他者评价不能冒充独立第三方来源；明确推断应标 inference，聊天转述应标 reported 并注明限制")


class NuwaWorkflow:
    def __init__(self, llm, jobs, job_id, payload, target, person, persona, observations, stats, quotes, summary_only):
        self.llm, self.jobs, self.job_id, self.payload = llm, jobs, job_id, payload
        self.target, self.person, self.persona = target, person, persona
        self.observations, self.stats, self.quotes, self.summary_only = observations, stats, quotes, summary_only
        self.root = target / "nuwa"
        self.root.mkdir(exist_ok=True)
        self.semaphore = asyncio.Semaphore(llm.settings(payload)["concurrency"])
        self.index = EvidenceIndex(target)
        previous = read_json(self.root / "parameters.json")
        legacy_packet = read_json(target / "skill" / "references" / "evidence.json") if previous else None
        # Resume earlier paid requests against their original immutable packet.
        self.legacy = bool(previous and "dimension_packets_sha256" not in previous)
        self.index.build(lambda: self.jobs.check(job_id))
        self.packet = legacy_packet if self.legacy and legacy_packet else (
            corpus_packet(target, observations, summary_only) if self.legacy else self.index.packet(summary_only))
        self.dimension_packets = {} if self.legacy else {file: self.index.packet(summary_only, DIMENSION_FILTERS[file], 2000, 3500)
                                                       for file, _ in DIMENSIONS}
        self.allowed = {m["id"] for m in self.packet["evidence_messages"]}
        self.research_dir = target / "skill" / "references" / "research"
        self.research_dir.mkdir(parents=True, exist_ok=True)

    async def request(self, name, file, context, validator=None, *, normalizer=None):
        async with self.semaphore:
            self.jobs.check(self.job_id)
            cache = read_json(self.root / file)
            if cache is not None:
                return cache
            if name == "nuwa_respond":
                # The blind actor sees only Skill text and questions. Its IDs
                # are embedded citations, not JSON evidence fields. Never use
                # expected answers or the whole corpus to widen its allowlist.
                cited = citation_ids(context["skill_markdown"])
                allowed = {m["id"] for m in self.index.messages(cited)}
            else:
                allowed = ids_in(context) | {m["id"] for m in context.get("evidence_messages", [])}
            result = await self.llm.call(name, context, allowed, self.job_id, self.payload, validator, normalizer=normalizer)
            write_json(self.root / file, result)
            return result

    def checkpoint(self, key, title, summary, progress):
        checkpoints = read_json(self.root / "checkpoints.json", {})
        checkpoints[key] = {"title": title, "summary": summary, "at": checkpoints.get(key, {}).get("at", now())}
        write_json(self.root / "checkpoints.json", checkpoints)
        self.jobs.progress(self.job_id, title, progress, nuwa={"checkpoints": checkpoints})
        payload = self.jobs.get(self.job_id)["payload"]
        if payload.get("nuwa_review") and key not in payload.get("nuwa_approved", []):
            self.jobs.update_payload(self.job_id, nuwa_pending=key)
            self.jobs.db.execute("UPDATE jobs SET status='paused',stage=? WHERE id=?", (title + " · 请查看摘要后继续", self.job_id))
            raise JobStopped()

    async def research(self):
        self.jobs.progress(self.job_id, "女娲 · 六维本地语料调研", .74)
        async def dimension(filename, label):
            packet = self.dimension_packets.get(filename, self.packet)
            def validate(result):
                validate_research_sources(result, packet["evidence_messages"], filename)
            output = await self.request("nuwa_research", filename.replace(".md", ".json"),
                                        {**packet, "dimension": label, "file": filename}, validate,
                                        normalizer=lambda result: normalize_research_sources(result, packet["evidence_messages"]))
            atomic_write(self.research_dir / filename, research_markdown(label, output))
            return {"file": filename, "dimension": label, **output}
        tasks = [asyncio.create_task(dimension(*entry)) for entry in DIMENSIONS]
        try:
            research = await asyncio.gather(*tasks)
        except BaseException:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise
        summary = [{"dimension": x["dimension"], "findings": len(x["findings"]), "sources": len(ids_in(x)),
                    "primary": sum(i["source_kind"] == "direct" for i in x["findings"]), "gaps": x["gaps"],
                    "contradictions": [i["value"] for i in x["contradictions"]]} for x in research]
        self.checkpoint("research", "女娲 · 调研质量摘要", summary, .77)
        return research, summary

    def validate_synthesis(self, output):
        for item in output["candidates"]:
            if any(not set(scene["evidence"]) <= set(item["evidence"]) for scene in item["scenarios"]):
                raise ValueError("候选模型 evidence 必须包含全部场景证据")
        if self.summary_only and any(output[k] for k in ("candidates", "heuristics", "values", "lineage")):
            raise ValueError("匿名统计不足以建立人物观点，候选/启发式/价值观/谱系必须留空")

    async def test(self, label, markdown, plan):
        questions = [{"id": q["id"], "question": q["question"]} for q in plan["known"]]
        questions += [{"id": "edge", "question": plan["edge_question"]}, {"id": "voice", "question": plan["voice_question"]}]
        expected_ids = {x["id"] for x in questions}
        def complete(field):
            def validate(output):
                items = output[field]
                if len(items) != len(expected_ids) or {x["id"] for x in items} != expected_ids:
                    raise ValueError("测试必须逐一覆盖给定问题，ID不能重复或缺失")
            return validate
        # Crucially, the role player cannot see the expected answers or corpus.
        answers = await self.request("nuwa_respond", label + "-answers.json", {"skill_markdown": markdown, "questions": questions}, complete("answers"))
        evaluation = await self.request("nuwa_evaluate", label + "-evaluation.json",
                                        {**self.packet, "research": self.research_context, "plan": plan, "answers": answers}, complete("checks"))
        checks = list(evaluation["checks"])
        answer_map = {a["id"]: a for a in answers["answers"]}
        edge, voice = answer_map["edge"], answer_map["voice"]
        cautious = edge["inferred"] and any(word in edge["text"] for word in ("推断", "不确定", "未知", "依据不足", "没有足够"))
        checks.append({"id": "edge-label", "passed": cautious, "reason": "未知问题须明确标记推断或证据不足。", "evidence": []})
        copied = any(len(m["text"]) >= 25 and m["text"] in voice["text"] for m in self.packet["evidence_messages"])
        checks.append({"id": "voice-length", "passed": 60 <= len(voice["text"]) <= 180 and not copied,
                       "reason": "风格样本需约100字，且不可直接拼贴较长原话；语气辨识由独立模型评价。", "evidence": []})
        checks.append({"id": "known-coverage", "passed": len(plan["known"]) == 3,
                       "reason": f"获得 {len(plan['known'])}/3 个有明确原文立场的已知测试。", "evidence": []})
        return {"checks": checks, "passed": all(x["passed"] for x in checks), "improvements": evaluation["improvements"],
                "answers": answers["answers"], "plan": plan,
                "skill_sha256": hashlib.sha256((self.target / "skill" / "SKILL.md").read_bytes()).hexdigest()}

    def content_checks(self, skill):
        findings = [f for r in self.research_data for f in r["findings"]]
        primary = sum(f["source_kind"] == "direct" for f in findings)
        return [
            {"id": "model-coverage", "passed": 3 <= len(skill["models"]) <= 7, "reason": f"{len(skill['models'])} 个模型通过三重检验；标准范围3–7，缺少时诚实降级。", "evidence": []},
            {"id": "tension-coverage", "passed": len(skill["tensions"]) >= 2, "reason": f"保留 {len(skill['tensions'])} 条有证据的张力；不足2条时标明缺口，禁止虚构矛盾。", "evidence": []},
            {"id": "primary-coverage", "passed": primary / max(1, len(findings)) > .5, "reason": f"研究结论中一手来源 {primary}/{len(findings)}；转述不计独立证实。", "evidence": []},
        ]

    def render(self, skill, additions=None):
        from echotrace.distill.skill_writer import write_skill
        write_json(self.target / "skill_model.json", skill)
        write_json(self.root / "additions.json", additions or [])
        write_skill(self.target, self.person, self.persona, skill, self.observations, self.stats, self.quotes)
        return (self.target / "skill" / "SKILL.md").read_text(encoding="utf-8")

    def close_portable_evidence(self):
        """Persist full current-version messages for every emitted citation."""
        if self.summary_only:
            return
        references = self.target / "skill" / "references"
        markdown_paths = [self.target / "skill" / "SKILL.md", references / "quotes.md"]
        markdown_paths.extend(sorted((references / "research").glob("*.md")))
        cited = set()
        for path in markdown_paths:
            if path.is_file():
                cited.update(citation_ids(path.read_text(encoding="utf-8")))
        messages = {message["id"]: message for message in self.packet["evidence_messages"]}
        messages.update({message["id"]: message for message in self.index.messages(cited)})
        packet = {**self.packet, "evidence_messages": sorted(
            messages.values(), key=lambda message: (message.get("sequence", 0), message["id"])
        )}
        write_json(references / "evidence.json", packet)

    async def run(self):
        settings = self.llm.settings(self.payload)
        parameters = {"method": METHOD, "model": settings["model"], "thinking": settings["thinking"],
                      "summary_only": self.summary_only, "sources": method_sources(),
                      "prompts": {k: hashlib.sha256(self.llm.template(k).encode()).hexdigest() for k in STAGES},
                      "packet_sha256": hashlib.sha256(dumps(self.packet).encode()).hexdigest()}
        if not self.legacy:
            parameters["dimension_packets_sha256"] = hashlib.sha256(dumps(self.dimension_packets).encode()).hexdigest()
        previous = read_json(self.root / "parameters.json")
        if previous and previous != parameters:
            raise AppError("NUWA_SETTINGS_CHANGED", "女娲阶段的提示词或证据已变化，请恢复设置或仅重跑 Skill 新版本；已有分片仍保留。")
        write_json(self.root / "parameters.json", parameters)
        write_json(self.target / "skill" / "references" / "evidence.json", self.packet)
        if self.dimension_packets:
            write_json(self.target / "skill" / "references" / "dimension-evidence.json", self.dimension_packets)
        research, summary = await self.research()
        self.research_data = research
        # Full research remains in six checkpoint files. Synthesis receives a
        # bounded cross-dimension selection, with contradictions selected first.
        self.research_context = research if self.legacy else [
            {**entry, "contradictions": limited(entry["contradictions"], 800),
             "findings": limited(entry["findings"], 1200), "gaps": limited(entry["gaps"], 200)} for entry in research]
        context = {**self.packet, "research": self.research_context}
        def validate_plan(output):
            if self.summary_only and output["known"]:
                raise ValueError("匿名统计无已知立场，不得构造已知问题")
            if any(q["expected"]["inferred"] for q in output["known"]):
                raise ValueError("已知测试必须基于明确表态，不能使用推断")
            if any(not set(q["expected"]["evidence"]) <= self.allowed for q in output["known"]):
                raise ValueError("已知测试只能选择 evidence_messages 中可供独立评审复核的原话")
        plan = await self.request("nuwa_test_plan", "test-plan.json", context, validate_plan)
        rounds, best, feedback = [], None, []
        for iteration in range(3):
            self.jobs.progress(self.job_id, f"女娲 · 框架提炼与独立验证 {iteration + 1}/3", .79 + iteration * .025)
            synthesis = await self.request("nuwa_synthesize", f"synthesis-{iteration}.json", {**context, "feedback": feedback}, self.validate_synthesis)
            skill = select_framework(synthesis, self.summary_only)
            self.checkpoint("synthesis", "女娲 · 提炼质量摘要", {"models": len(skill["models"]), "heuristics": len(skill["heuristics"]),
                            "selection": skill["selection"], "unknowns": skill["unknowns"]}, .8)
            markdown = self.render(skill)
            tested = await self.test(f"round-{iteration}", markdown, plan)
            tested["checks"] += self.content_checks(skill)
            tested["passed"] = all(x["passed"] for x in tested["checks"])
            format_result = read_json(self.target / "skill_quality.json")
            passed = tested["passed"] and format_result["passed"] and not self.summary_only
            rank = (int(passed), sum(x["passed"] for x in tested["checks"]) + sum(x["passed"] for x in format_result["checks"]))
            record = {"iteration": iteration, "validation": tested, "format": format_result, "passed": passed}
            rounds.append(record)
            if best is None or rank > best[0]:
                best = (rank, skill, tested, format_result, markdown)
            if passed:
                break
            # No extra model calls can recover evidence absent from the supplied corpus.
            if self.summary_only or not self.allowed or len(plan["known"]) < 3:
                break
            feedback = tested["improvements"] + [x["reason"] for x in tested["checks"] if not x["passed"]] + [x["name"] for x in format_result["checks"] if not x["passed"]]
        _, skill, validation, format_result, markdown = best
        self.render(skill)
        self.jobs.progress(self.job_id, "女娲 · 双视角独立精炼", .87)
        reviews = []
        # A separate request/history for each review, bounded by the same concurrency/budget.
        for perspective in ("optimizer", "creator"):
            def validate_review(output, expected=perspective):
                if output["perspective"] != expected:
                    raise ValueError("精炼报告视角不匹配")
                if expected == "optimizer" and (len(output["scores"]) != 8 or len(output["dry_runs"]) != 3):
                    raise ValueError("optimizer须有8维评分和3个干跑结果")
            reviews.append(await self.request("nuwa_refine", perspective + ".json", {"perspective": perspective,
                           "skill_markdown": markdown, "validation": validation}, validate_review))
        proposals, seen = [], set()
        for review in reviews:
            for change in review["suggestions"]:
                if change["text"].strip() not in seen:
                    seen.add(change["text"].strip())
                    proposals.append({**change, "perspective": review["perspective"]})
        additions, rejected = [], []
        integration = None
        if proposals:
            def validate_integration(output):
                if len(output["decisions"]) != len(proposals) or {d["index"] for d in output["decisions"]} != set(range(len(proposals))):
                    raise ValueError("必须逐一评估全部精炼建议，不得重复或遗漏")
            integration = await self.request("nuwa_integrate", "integration.json", {"skill_markdown": markdown, "proposals": proposals}, validate_integration)
            for decision in integration["decisions"]:
                change = {**proposals[decision["index"]], "integration_reason": decision["reason"]}
                (additions if decision["accepted"] else rejected).append(change)
        applied = False
        if additions:
            before_format = read_json(self.target / "skill_quality.json")
            refined_markdown = self.render(skill, additions)
            refined = await self.test("refined", refined_markdown, plan)
            refined["checks"] += self.content_checks(skill)
            refined["passed"] = all(x["passed"] for x in refined["checks"])
            # Keep the previously verified result if refinement regresses any passing check.
            before = {x["id"] for x in validation["checks"] if x["passed"]}
            after = {x["id"] for x in refined["checks"] if x["passed"]}
            after_format = read_json(self.target / "skill_quality.json")
            format_before = {x["name"] for x in before_format["checks"] if x["passed"]}
            format_after = {x["name"] for x in after_format["checks"] if x["passed"]}
            if before <= after and format_before <= format_after:
                validation, markdown, applied = refined, refined_markdown, True
            else:
                self.render(skill)
                rejected += [{**x, "integration_reason": "精炼后出现验证退步，保留原版本。"} for x in additions]
        format_result = read_json(self.target / "skill_quality.json")
        gaps = [f"{r['dimension']}：{gap}" for r in research for gap in r["gaps"]]
        gaps += skill["unknowns"]
        if len(skill["models"]) < 3:
            gaps.append(f"只有 {len(skill['models'])} 个候选通过三重检验；未补造模型以满足数量。")
        if self.summary_only:
            gaps.append("摘要模式没有原始观点与语气，不能验证人物立场或还原表达风格。")
        self.close_portable_evidence()
        passed = validation["passed"] and format_result["passed"] and not self.summary_only
        report = {"method": METHOD, "mode": "local_only", "status": "passed" if passed else "degraded", "completed_at": now(),
                  "research": summary, "selection": skill["selection"], "models": len(skill["models"]), "heuristics": len(skill["heuristics"]),
                  "gaps": gaps, "validation": validation, "rounds": rounds, "repair_rounds": len(rounds)-1,
                  "refinement": {"reviews": reviews, "integration": integration, "applied": additions if applied else [], "rejected": rejected},
                  "provenance": parameters, "checkpoints": read_json(self.root / "checkpoints.json", {}),
                  "note": "蒸馏模型已加载女娲 Skill 原文、提炼框架与人物模板；纯本地语料模式。六维研究、扮演和评审分别请求；语义验证并非本人认证，格式检查另列。"}
        self.checkpoint("validation", "女娲 · 验证与精炼摘要", {"status": report["status"], "checks": validation["checks"],
                        "gaps": gaps, "applied": report["refinement"]["applied"]}, .91)
        report["checkpoints"] = read_json(self.root / "checkpoints.json", {})
        write_json(self.target / "nuwa_report.json", report)
        write_json(self.target / "skill" / "references" / "nuwa-report.json", report)
        quality = {**format_result, "semantic_passed": passed, "nuwa_status": report["status"], "validation": validation["checks"]}
        write_json(self.target / "skill_quality.json", quality)
        write_json(self.target / "skill" / "references" / "skill-quality.json", quality)
        atomic_write(self.target / "skill" / "references" / "workflow.md", "# 女娲流程记录\n\n" +
                     f"方法：{METHOD}；纯本地语料；修复 {len(rounds)-1}/2 轮。\n\n" +
                     "\n".join("- " + x for x in gaps) + "\n\n具体调研、候选筛选、测试问答及精炼报告见 nuwa-report.json。\n")
        return skill, quality, report
