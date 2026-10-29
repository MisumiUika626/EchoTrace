import asyncio
import hashlib
import shutil
import time
from dataclasses import asdict
from datetime import datetime, timedelta
from pathlib import Path

from echotrace.distill.canonical_skill import build_distill_manifest, validate_canonical_skill
from echotrace.distill.evidence import EvidenceIndex
from echotrace.distill.extraction import extract
from echotrace.distill.full_reduce import map_chunk, reduce_findings
from echotrace.distill.nuwa import NuwaWorkflow
from echotrace.distill.persona import person, read_quotes
from echotrace.distill.planner import PLANNER_VERSION, budgets_for_views, plan_full_chunks
from echotrace.distill.radar import score, timeline_events
from echotrace.distill.skill_writer import write_skill
from echotrace.ingest.canonical import canonical_messages, message_fingerprint, model_message
from echotrace.ingest.parsers import TZ
from echotrace.llm.client import estimate_tokens
from echotrace.rag.indexer import from_model, rebuild
from echotrace.stats import calculate
from echotrace.util import AppError, atomic_write, dumps, finish_thread, now, read_json, write_json


def checkpoint_parameters(path, current, message):
    saved = read_json(path)
    if saved is not None:
        # Output allowance can change without invalidating validated results.
        # Keep the original identity so partial chunk fingerprints remain usable.
        saved_identity = {key: value for key, value in saved.items() if key != "max_tokens"}
        current_identity = {key: value for key, value in current.items() if key != "max_tokens"}
        # New prompt stages must not invalidate otherwise compatible legacy checkpoints.
        # Prompt identities that existed when the checkpoint was written remain strict.
        if isinstance(saved_identity.get("prompts"), dict) and isinstance(current_identity.get("prompts"), dict):
            current_prompts = current_identity["prompts"]
            current_identity["prompts"] = {
                key: current_prompts.get(key) for key in saved_identity["prompts"]
            }
        if saved_identity != current_identity:
            raise AppError("RESUME_SETTINGS_CHANGED", message)
        return saved
    write_json(path, current)
    return current


def evidence_ids(value):
    ids = set()
    if isinstance(value, dict):
        for k, v in value.items():
            if k == "evidence":
                ids.update(v)
            else:
                ids.update(evidence_ids(v))
    elif isinstance(value, list):
        for v in value:
            ids.update(evidence_ids(v))
    return ids


def compact_observations(items, budget=22000):
    seen, result, used = set(), [], 0
    # Spread evidence over the full time range, rather than keeping only the start.
    stride = max(1, len(items) // 250)
    ordered = items[::stride] + items
    for item in ordered:
        key = (item["value"], item.get("date"), tuple(sorted(item.get("evidence", []))), item.get("dimension"))
        cost = estimate_tokens(item)
        if key in seen or used + cost > budget:
            continue
        seen.add(key)
        result.append(item)
        used += cost
    return result


def summary_messages(messages):
    # No original text, names, addresses or snippets leave the machine in summary mode.
    return [{"id": m["id"], "sender": "A" if m["sender"] == "me" else "B", "date": datetime.fromtimestamp(m["ts"], TZ).date().isoformat(),
             "characters": m.get("source_characters", len(m["text"])), "kind": m["kind"],
             "text": f"[匿名统计：{m['kind']}消息，{m.get('source_characters', len(m['text']))}字]"} for m in messages]


def bounded_examples(messages, budget=5000):
    selected, used = [], 0
    for message in messages:
        excerpt = {**message, "text": message["text"][:1000]}
        if len(message["text"]) > 1000:
            excerpt.update(fragment_offset=message.get("fragment_offset", 0),
                           source_characters=message.get("source_characters", len(message["text"])))
        cost = estimate_tokens(excerpt)
        if used + cost <= budget:
            selected.append(excerpt)
            used += cost
    return selected


def _scoped_rows(db, contact_id, scope):
    rows = canonical_messages(db, contact_id)["messages"]
    return [row for row in rows
            if (not scope.get("start") or row["ts"] >= int(scope["start"]))
            and (not scope.get("end") or row["ts"] <= int(scope["end"]))]


def validate_full_coverage(db, contact_id, target, manifest):
    """Validate persisted input views against canonical storage, then return full rows."""
    def invalid(detail):
        raise AppError("INVALID_FULL_COVERAGE", f"完整分片覆盖校验失败：{detail}")

    rows = _scoped_rows(db, contact_id, manifest.get("scope") or {})
    if manifest.get("source_fingerprint") != message_fingerprint(rows):
        invalid("源消息指纹已变化")
    expected = {row["evidence_id"]: model_message(row) for row in rows}
    covered, covered_ids, order = {}, set(), []
    filenames = manifest.get("chunks", [])
    descriptors = manifest.get("chunk_descriptors", [])
    if (not filenames or len(filenames) != len(descriptors)
            or len(set(filenames)) != len(filenames)):
        invalid("分片清单结构不完整")
    for index, filename in enumerate(filenames):
        descriptor = descriptors[index]
        views = read_json(target / "inputs" / filename)
        if not isinstance(views, list) or not views:
            invalid("分片输入为空或格式错误")
        new_indexes = descriptor.get("new_indexes", [])
        if not new_indexes or len(set(new_indexes)) != len(new_indexes):
            invalid("分片缺少唯一首次覆盖索引")
        if any(position < 0 or position >= len(views) for position in new_indexes):
            invalid("首次覆盖索引越界")
        for position, view in enumerate(views):
            if position in new_indexes:
                continue
            source = expected.get(view.get("id"))
            if (source is None or view.get("id") not in covered_ids
                    or "fragment_index" in view or view != source):
                invalid("重叠只能复制授权范围内已首次覆盖的完整原消息")
        overlap_count = len(views) - len(new_indexes)
        if overlap_count > 3 or descriptor.get("overlap_messages") != overlap_count:
            invalid("重叠数量与规划描述不一致")
        for position in new_indexes:
            view = views[position]
            evidence_id = view.get("id")
            if evidence_id not in expected:
                invalid("Evidence ID 不属于授权范围")
            key = (evidence_id, view.get("fragment_index"))
            if key in covered:
                invalid("首次覆盖重复")
            covered[key] = view
            covered_ids.add(evidence_id)
            order.append((view.get("sequence"), view.get("fragment_index", -1)))
    if order != sorted(order):
        invalid("首次覆盖顺序变化")
    for evidence_id, source in expected.items():
        items = [view for (item_id, _), view in covered.items() if item_id == evidence_id]
        if not items:
            invalid(f"{evidence_id} 未覆盖")
        if len(items) == 1 and "fragment_index" not in items[0]:
            if items[0] != source:
                invalid(f"{evidence_id} 正文或元数据变化")
            continue
        items.sort(key=lambda item: item.get("fragment_index", -1))
        count = len(items)
        if [item.get("fragment_index") for item in items] != list(range(count)):
            invalid(f"{evidence_id} 片段索引不连续")
        if any(item.get("fragment_count") != count for item in items):
            invalid(f"{evidence_id} 片段总数不一致")
        offset = 0
        for item in items:
            if item.get("fragment_offset") != offset or not item.get("text"):
                invalid(f"{evidence_id} 片段偏移错误")
            comparable = {key: value for key, value in item.items()
                          if key not in ("text", "fragment_index", "fragment_count", "fragment_offset")}
            if comparable != {key: value for key, value in source.items() if key != "text"}:
                invalid(f"{evidence_id} 片段元数据变化")
            offset += len(item["text"])
        if "".join(item["text"] for item in items) != source["text"]:
            invalid(f"{evidence_id} 片段无法逐字重组")
    if len({item_id for item_id, _ in covered}) != len(expected):
        invalid("覆盖范围与授权范围不一致")
    return [model_message(row) for row in rows]


def resolve_distill_mode(payload):
    """Missing mode is a persisted pre-upgrade job and must remain sampled."""
    return payload.get("mode") or "sampled"


def _active_complete(db, contact_id):
    row = db.one("SELECT dir FROM personas WHERE contact_id=? AND active=1", (contact_id,))
    return bool(row and read_json(Path(row["dir"]) / "metadata.json", {}).get("complete") is True)


def assert_distill_start_allowed(db, config, contact_id, payload):
    mode = resolve_distill_mode(payload)
    privacy = config.get()["privacy"]
    raw = bool(payload.get("send_raw", True) and privacy["allow_raw"] and not privacy["summary_only"])
    if mode == "full" and not raw:
        raise AppError("FULL_REQUIRES_RAW_TEXT", "full 模式需要授权范围内的完整原文。")
    if mode == "sampled" and _active_complete(db, contact_id):
        raise AppError("SAMPLED_CANNOT_REPLACE_COMPLETE",
                       "sampled 预览不能替换已经 complete 的正式人物版本。")
    return mode


class Pipeline:
    def __init__(self, db, config, jobs, llm):
        self.db, self.config, self.jobs, self.llm = db, config, jobs, llm

    def estimate(self, cid, tier="standard"):
        row = self.db.one("SELECT msg_count FROM sessions WHERE contact_id=?", (cid,))
        n = row["msg_count"] if row else 0
        tokens = max(300000, n * (60 if tier == "deep" else 40) + 280000)
        return {"messages": n, "estimated_tokens": tokens, "suggested_limit": int(tokens * 1.5),
                "estimated_minutes": [max(2, n // 1000), max(5, n // 200)],
                "note": "模型加载女娲 Skill 原文并执行六维研究、筛选、独立测试及双视角精炼；基础12次请求，有建议整合及复验时15次，每轮修复另加3次，最多2轮。实际依文本、重试与思考用量变化；费用以服务商为准。"}

    def prepare(self, cid, target, payload, stats, check=lambda: None):
        root = target / "inputs"
        root.mkdir(parents=True, exist_ok=True)
        existing = read_json(target / "manifest.json")
        if existing:
            source = person(self.db, cid)
            if existing.get("source_revision") != source["revision"]:
                raise AppError("RESUME_SOURCE_CHANGED", "聊天记录或发言人已变化，旧检查点不能继续。请取消旧任务并重新蒸馏。")
            return existing
        first_month = datetime.fromtimestamp(stats["first_ts"], TZ).strftime("%Y-%m")
        last_month = stats["last_ts"] - 30 * 86400
        interval = {"quick": 10, "standard": 5, "deep": 1}[payload.get("tier", "standard")]
        paths, chunk, tokens, previous_ts, count = [], [], 0, None, 0
        self_examples = []
        scope = payload.get("scope") or {}
        def save_chunk(messages):
            filename = f"{len(paths):05d}.json"
            write_json(root / filename, messages)
            paths.append(filename)
        messages = canonical_messages(self.db, cid)["messages"]
        for offset, source in enumerate(messages):
            if scope.get("start") and source["ts"] < int(scope["start"]):
                continue
            if scope.get("end") and source["ts"] > int(scope["end"]):
                continue
            if offset % 256 == 0:
                check()
            message = {"id": source["evidence_id"], "sequence": source["sequence"],
                       "ts": source["ts"], "sender": source["sender"],
                       "text": source["text"], "kind": source["kind"]}
            ts, text = message["ts"], message["text"]
            mandatory = (datetime.fromtimestamp(ts, TZ).strftime("%Y-%m") == first_month or ts >= last_month
                         or (previous_ts is not None and ts - previous_ts > 30 * 86400)
                         or any(w in text for w in ("生气", "抱歉", "难过", "答应", "喜欢", "再见", "承诺")))
            previous_ts = ts
            if not mandatory and message["sequence"] % interval != 0:
                continue
            count += 1
            if message["sender"] == "me":
                if len(self_examples) < 120:
                    self_examples.extend(bounded_examples([message]))
                elif message["sequence"] % 11 == 0:
                    self_examples[count % 120] = message
            cost = estimate_tokens(message)
            if chunk and tokens + cost > 10000:
                save_chunk(chunk)
                overlap, overlap_tokens = [], 0
                for old in reversed(chunk):
                    if overlap_tokens + estimate_tokens(old) > 300:
                        break
                    overlap.insert(0, old)
                    overlap_tokens += estimate_tokens(old)
                chunk, tokens = overlap, overlap_tokens
            chunk.append(message)
            tokens += cost
        if chunk:
            save_chunk(chunk)
        if stats["total"] < 200:
            raise AppError("NOT_ENOUGH_DATA", "所选时间范围不足200条消息，请扩大范围。")
        manifest = {"chunks": paths, "sampled": count, "total": stats["total"], "self_examples": self_examples,
                    "source_revision": person(self.db, cid)["revision"],
                    "source_count": self.db.one("SELECT msg_count FROM sessions WHERE contact_id=?", (cid,))["msg_count"]}
        write_json(target / "manifest.json", manifest)
        return manifest

    def prepare_full(self, cid, target, payload, stats, settings, target_name, check=lambda: None):
        root = target / "inputs"
        root.mkdir(parents=True, exist_ok=True)
        existing = read_json(target / "manifest.json")
        source = person(self.db, cid)
        if existing:
            if existing.get("source_revision") != source["revision"]:
                raise AppError("RESUME_SOURCE_CHANGED", "聊天记录或发言人已变化，旧检查点不能继续。请取消旧任务并重新蒸馏。")
            if existing.get("planner_version") != PLANNER_VERSION:
                raise AppError("RESUME_SETTINGS_CHANGED", "完整分片检查点的规划器版本已变化，请新建任务。")
            validate_full_coverage(self.db, cid, target, existing)
            return existing
        scope = payload.get("scope") or {}
        rows = _scoped_rows(self.db, cid, scope)
        plan = plan_full_chunks(self.llm, rows, settings, target_name)
        paths, descriptors = [], []
        for index, chunk in enumerate(plan.chunks):
            check()
            filename = f"{index:05d}.json"
            write_json(root / filename, list(chunk.views))
            paths.append(filename)
            descriptors.append({
                "index": index,
                "new_indexes": list(chunk.new_indexes),
                "sequence_start": min(chunk.views[position]["sequence"] for position in chunk.new_indexes),
                "sequence_end": max(chunk.views[position]["sequence"] for position in chunk.new_indexes),
                "overlap_messages": len(chunk.views) - len(chunk.new_indexes),
                "stage_budgets": {stage: asdict(budget) for stage, budget in chunk.budgets.items()},
            })
        views = [model_message(row) for row in rows]
        manifest = {
            "chunks": paths, "chunk_descriptors": descriptors,
            "sampled": len(rows), "total": stats["total"],
            "self_examples": bounded_examples([view for view in views if view["sender"] == "me"]),
            "source_revision": source["revision"],
            "source_count": self.db.one("SELECT msg_count FROM sessions WHERE contact_id=?", (cid,))["msg_count"],
            "source_fingerprint": plan.source_fingerprint, "scope": scope,
            "planner_version": plan.planner_version, "plan_mode": plan.mode,
            "fragmented": plan.fragmented,
            "map_total": len(paths), "map_succeeded": 0, "failed_chunks": [],
            "reduce_layers": None, "evidence_validated": False,
            "phase4_complete": False,
        }
        write_json(target / "manifest.json", manifest)
        validate_full_coverage(self.db, cid, target, manifest)
        return manifest

    async def run(self, job_id, payload):
        # Long jobs use per-request timeouts and durable checkpoints. A healthy
        # 500K-message run must not expire just because 30 minutes have elapsed.
        if payload.get("skill_only"):
            return await self._run_skill(job_id, payload)
        return await self._run(job_id, payload)

    async def _run_skill(self, job_id, payload):
        cid = payload["contact_id"]
        contact = person(self.db, cid)
        local_only = payload.get("local_only", False)
        settings = None if local_only else self.llm.settings(payload)
        if not local_only and not payload.get("consent"):
            raise AppError("CONSENT_REQUIRED", "重跑 Skill 需要确认本次向模型发送画像和证据摘要。")
        source_row = self.db.one("SELECT dir FROM personas WHERE contact_id=? AND version=?", (cid, payload["base_version"]))
        if not source_row:
            raise AppError("NOT_FOUND", "用于重跑的历史版本不存在。", 404)
        source = Path(source_row["dir"])
        previous = read_json(source / "metadata.json")
        if previous["source_revision"] != contact["revision"]:
            raise AppError("RESUME_SOURCE_CHANGED", "原始记录已变化，请先运行完整蒸馏。")
        privacy = self.config.get()["privacy"]
        if not local_only and not previous["summary_only"] and (privacy["summary_only"] or not privacy["allow_raw"]):
            raise AppError("PRIVACY_MODE_CHANGED", "当前隐私设置禁止发送原文摘要，请先使用摘要模式重新蒸馏。")
        root = source.parent
        version = payload.get("version")
        if version is None:
            version = max(int(p.name[1:]) for p in root.glob("v*") if p.name[1:].isdigit()) + 1
            self.jobs.update_payload(job_id, version=version)
        target = root / f"v{version}"
        if not target.exists():
            def copy_source():
                staging = root / f".skill-copy-{job_id}"
                if staging.exists():
                    shutil.rmtree(staging)
                shutil.copytree(source, staging)
                if not local_only:
                    (staging / "skill_model.json").unlink(missing_ok=True)
                    shutil.rmtree(staging / "nuwa", ignore_errors=True)
                    (staging / "nuwa_report.json").unlink(missing_ok=True)
                    shutil.rmtree(staging / "skill" / "references" / "research", ignore_errors=True)
                (staging / "parameters.json").unlink(missing_ok=True)
                # Windows scanners may briefly hold the newly copied directory.
                # Retry only this publication step; permanent access errors still fail.
                for attempt, delay in enumerate((.05, .1, .2, .4, .8, 0)):
                    try:
                        staging.rename(target)
                        break
                    except PermissionError:
                        if attempt == 5:
                            raise
                        time.sleep(delay)
            await finish_thread(copy_source)
        parameters = {} if local_only else {key: settings[key] for key in ("model", "thinking", "base_url", "adapter", "thinking_values", "thinking_field", "temperature", "max_tokens")}
        parameters.update(base_version=payload["base_version"], skill_only=True,
                          local_only=local_only)
        if not local_only:
            parameters["prompt"] = hashlib.sha256(self.llm.template("distill_skill").encode()).hexdigest()
        parameters = checkpoint_parameters(target / "parameters.json", parameters,
            "Skill 检查点的模型或提示词已变化，请恢复设置或新建任务。")
        self.jobs.progress(job_id, "本地重建 Skill 新版本" if local_only else "重跑 Skill 生成阶段", .3)
        persona, observations = read_json(target / "persona.json"), read_json(target / "observations.json")
        skill = read_json(target / "skill_model.json")
        if local_only and not skill:
            raise AppError("INCOMPLETE_ARTIFACTS", "历史版本缺少 Skill 结构数据，请重新蒸馏。")
        skill_contact = {**contact, "display_name": "B", "slug": f"anonymous-{cid:x}"} if previous["summary_only"] else contact
        if local_only:
            quality = await finish_thread(write_skill, target, skill_contact, persona, skill, observations, read_json(target / "stats.json"), read_quotes(target))
            report = read_json(target / "nuwa_report.json")
            if report:
                matches = hashlib.sha256((target / "skill" / "SKILL.md").read_bytes()).hexdigest() == report["validation"]["skill_sha256"]
                if not matches:
                    report.update(status="stale", note="本地重建后的 Skill 内容已变化，原语义验证不适用于此版本；使用模型重跑 Skill 重新验证。")
                    write_json(target / "nuwa_report.json", report)
                    write_json(target / "skill" / "references" / "nuwa-report.json", report)
                quality.update(semantic_passed=matches and report["status"] == "passed", nuwa_status=report["status"],
                               validation=report["validation"]["checks"] if matches else [])
                write_json(target / "skill_quality.json", quality)
                write_json(target / "skill" / "references" / "skill-quality.json", quality)
        else:
            await finish_thread(EvidenceIndex(target).build, lambda: self.jobs.check(job_id))
            skill, quality, report = await NuwaWorkflow(self.llm, self.jobs, job_id, payload, target, skill_contact,
                persona, observations, read_json(target / "stats.json"), read_quotes(target), previous["summary_only"]).run()
        meta = {**previous, "version": version, "created_at": now(),
                "model": previous["model"] if local_only else settings["model"],
                "thinking": previous["thinking"] if local_only else settings["thinking"],
                "tokens": self.jobs.get(job_id)["tokens"], "job_id": job_id, "skill_quality_passed": quality["passed"], "skill_only": True, "base_version": payload["base_version"]}
        meta["local_only"] = local_only
        meta["nuwa_status"] = report["status"] if report else "legacy"
        meta.update(complete_eligible=False, complete=False)
        formal = read_json(target / "distill_manifest.json")
        if formal:
            formal["complete"] = False
            formal["gates"] = {**(formal.get("gates") or {}), "skill_only_revalidated": False}
            formal["failures"] = list(dict.fromkeys([
                *(formal.get("failures") or []),
                "skill_only 版本必须重新运行 full 才能恢复 complete",
            ]))
            formal["nuwa"] = {
                **(formal.get("nuwa") or {}),
                "status": meta["nuwa_status"],
                "format_passed": quality.get("passed"),
                "semantic_passed": quality.get("semantic_passed"),
            }
            write_json(target / "distill_manifest.json", formal)
        write_json(target / "metadata.json", meta)
        self.jobs.check(job_id)
        await finish_thread(self.publish, cid, target, meta)
        return {"contact_id": cid, "version": version, "quality": quality, "skill_only": True, "nuwa_status": meta["nuwa_status"]}

    async def _run(self, job_id, payload):
        cid = payload["contact_id"]
        contact = person(self.db, cid)
        root = self.db.root / "people" / contact["slug"]
        settings = self.llm.settings(payload)
        if not payload.get("consent"):
            raise AppError("CONSENT_REQUIRED", "本次蒸馏需要确认发送到所选模型服务。")
        send_raw = bool(payload.get("send_raw", True) and self.config.get()["privacy"]["allow_raw"] and not self.config.get()["privacy"]["summary_only"])
        distill_mode = assert_distill_start_allowed(self.db, self.config, cid, payload)
        version = payload.get("version")
        if version is None:
            folders = [int(p.name[1:]) for p in (root / "versions").glob("v*") if p.name[1:].isdigit()]
            version = max(folders, default=0) + 1
            self.jobs.update_payload(job_id, version=version)
        target = root / "versions" / f"v{version}"
        target.mkdir(parents=True, exist_ok=True)
        self.jobs.progress(job_id, "准备统计与分片", .03)
        scope = payload.get("scope") or {}
        stats = await asyncio.to_thread(calculate, self.db, cid, scope.get("start"), scope.get("end"))
        if stats["total"] < 200:
            raise AppError("NOT_ENOUGH_DATA", "至少需要200条消息才能蒸馏。")
        write_json(target / "stats.json", stats)
        recent = await asyncio.to_thread(calculate, self.db, cid, max(stats["first_ts"], stats["last_ts"] - 30 * 86400), stats["last_ts"])
        full_mode = distill_mode == "full"
        if full_mode:
            manifest = await finish_thread(
                self.prepare_full, cid, target, payload, stats, settings,
                contact["display_name"], lambda: self.jobs.check(job_id),
            )
        else:
            manifest = await finish_thread(self.prepare, cid, target, payload, stats,
                                           lambda: self.jobs.check(job_id))
        parameters = {"model": settings["model"], "thinking": settings["thinking"], "send_raw": send_raw,
                      "base_url": settings["base_url"], "adapter": settings["adapter"], "thinking_values": settings["thinking_values"],
                      "thinking_field": settings["thinking_field"], "temperature": settings["temperature"], "max_tokens": settings["max_tokens"],
                      "prompts": {name: hashlib.sha256(self.llm.template(name).encode()).hexdigest() for name in
                                  ("distill_chunk", "full_map", "full_reduce", "extract_quotes", "distill_persona", "distill_radar", "distill_skill", "self_style")},
                      "tier": payload.get("tier", "standard")}
        if "mode" in payload:
            parameters["distill_mode"] = distill_mode
        parameters["radar_scales"] = self.config.get()["radar"]["scales"]
        parameters = checkpoint_parameters(target / "parameters.json", parameters,
            "续跑参数与检查点不一致。请恢复原参数，或取消任务后新建蒸馏。")
        completed = 0
        map_states = {}
        async def process(filename):
            nonlocal completed
            self.jobs.check(job_id)
            messages = read_json(target / "inputs" / filename)
            if full_mode:
                descriptor = manifest["chunk_descriptors"][manifest["chunks"].index(filename)]
                current = {stage: asdict(budget) for stage, budget in
                           budgets_for_views(self.llm, messages, settings,
                                             contact["display_name"],
                                             [messages[index]["id"] for index in descriptor["new_indexes"]]).items()}
                if current != descriptor["stage_budgets"]:
                    raise AppError("PLANNER_REQUEST_MISMATCH",
                                   "完整分片的 Prompt、预算或模型容量已变化，已在发送前停止。")
            input_messages = messages if send_raw else summary_messages(messages)
            map_prompt = self.llm.template("full_map" if full_mode else "distill_chunk")
            fingerprint = hashlib.sha256(dumps([input_messages, parameters, map_prompt, self.llm.template("extract_quotes")]).encode()).hexdigest()
            cache = root / "cache" / (fingerprint + ".json")
            checkpoint = target / "chunks" / filename
            result = read_json(checkpoint) or read_json(cache)
            if not result:
                if full_mode:
                    descriptor = manifest["chunk_descriptors"][manifest["chunks"].index(filename)]
                    owned_ids = {messages[index]["id"] for index in descriptor["new_indexes"]}
                    state = await map_chunk(self.llm, self.jobs, job_id, payload, input_messages,
                                            owned_ids, target / "chunks" / (filename + ".map.json"), fingerprint)
                    map_states[filename] = state
                    if state["status"] != "succeeded":
                        completed += 1
                        return
                    observations = state["output"]
                else:
                    observations = await extract(self.llm, self.jobs, job_id, payload, "distill_chunk",
                        {"target_name": contact["display_name"] if send_raw else "B", "messages": input_messages, "summary_only": not send_raw},
                        target / "chunks" / (filename + ".observations.json"), fingerprint)
                quotes = await extract(self.llm, self.jobs, job_id, payload, "extract_quotes", {"messages": messages, "target": "them"},
                    target / "chunks" / (filename + ".quotes.json"), fingerprint) if send_raw else {"quotes": []}
                result = {"observations": observations["findings" if full_mode else "observations"], "quotes": quotes["quotes"]}
                if full_mode:
                    result["phase4_map_status"] = "succeeded"
                write_json(cache, result)
            if full_mode and filename not in map_states:
                map_states[filename] = {"status": result.get("phase4_map_status", "failed")}
            write_json(checkpoint, result)
            completed += 1
            self.jobs.progress(job_id, f"抽取证据 {completed}/{len(manifest['chunks'])}", .1 + .45 * completed / len(manifest["chunks"]))
        pending = iter(manifest["chunks"])
        async def worker():
            for filename in pending:
                await process(filename)
                await asyncio.sleep(0)
        tasks = [asyncio.create_task(worker()) for _ in range(min(settings["concurrency"], len(manifest["chunks"])))]
        try:
            await asyncio.gather(*tasks)
        except BaseException:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise
        canonical_payloads = validate_full_coverage(self.db, cid, target, manifest) if full_mode else None
        reduced_observations = None
        if full_mode:
            failed = [{"chunk": filename, "error": map_states.get(filename, {}).get("error", {"code": "MAP_INCOMPLETE"})}
                      for filename in manifest["chunks"] if map_states.get(filename, {}).get("status") != "succeeded"]
            manifest.update(map_total=len(manifest["chunks"]), map_succeeded=len(manifest["chunks"]) - len(failed),
                            failed_chunks=failed, reduce_layers=None, evidence_validated=not failed,
                            phase4_complete=False)
            write_json(target / "manifest.json", manifest)
            if failed:
                raise AppError("PHASE4_INCOMPLETE", "full Map 存在失败片；检查点和失败原因已保留。")
            mapped = [item for filename in manifest["chunks"]
                      for item in read_json(target / "chunks" / filename)["observations"]]
            reduce_fingerprint = hashlib.sha256(dumps([mapped, parameters, self.llm.template("full_reduce")]).encode()).hexdigest()
            reduced = await reduce_findings(self.llm, self.jobs, job_id, payload, mapped,
                                             canonical_payloads, target / "full_reduce.json", reduce_fingerprint)
            reduced_observations = [*reduced["output"]["findings"], *reduced["output"].get("contradictions", [])]
            manifest.update(reduce_layers=reduced["reduce_layers"], evidence_validated=True,
                            phase4_complete=True)
            write_json(target / "manifest.json", manifest)
        index = EvidenceIndex(target, canonical_payloads)
        await finish_thread(index.build, lambda: self.jobs.check(job_id))
        # Preserve the exact packet used by already completed aggregate checkpoints.
        observations = (reduced_observations if reduced_observations is not None
                        else read_json(target / "observations.json"))
        if observations is None:
            observations = await asyncio.to_thread(index.select)
        write_json(target / "observations.json", observations)
        ids = evidence_ids(observations)
        # Statistics do not expose raw names/content in summary mode.
        public_stats = {k: v for k, v in stats.items() if k not in ("aliases", "topics")}
        async def stage(name, filename, context, allowed, progress, validator=None):
            self.jobs.progress(job_id, name, progress)
            cached = read_json(target / filename)
            if cached is not None:
                return cached
            output = await self.llm.call(name, context, allowed, job_id, payload, validator)
            write_json(target / filename, output)
            return output
        persona = await stage("distill_persona", "persona.json", {"observations": observations, "stats": public_stats, "summary_only": not send_raw}, ids, .6)
        radar_context = {"persona": persona, "observations": observations, "stats": public_stats, "recent": {"anchors": recent["anchors"]}}
        radar_ids = evidence_ids(radar_context)
        radar_model = await stage("distill_radar", "radar_model.json", radar_context, radar_ids, .7,
                                  lambda x: score(x, stats, recent))
        radar = score(radar_model, stats, recent)
        write_json(target / "radar.json", radar)
        self_messages = manifest["self_examples"]
        self_messages = bounded_examples(self_messages if send_raw else summary_messages(self_messages))
        await stage("self_style", "self_style.json", {"messages": self_messages}, {m["id"] for m in self_messages}, .72)
        def collect_quotes():
            unique = {}
            limit = min(2000, max(1, round(stats["total"] * .03)))
            for filename in manifest["chunks"]:
                self.jobs.check(job_id)
                for quote in from_model(self.db, cid, read_json(target / "chunks" / filename)["quotes"]):
                    unique[quote["id"]] = quote
                unique = {q["id"]: q for q in sorted(unique.values(), key=lambda q: q["score"], reverse=True)[:limit]}
            return sorted(unique.values(), key=lambda q: q["score"], reverse=True)
        quotes = await finish_thread(collect_quotes)
        atomic_write(target / "quotes.jsonl", "".join(dumps(q) + "\n" for q in quotes))
        skill_contact = contact if send_raw else {**contact, "display_name": "B", "slug": f"anonymous-{cid:x}"}
        skill, quality, report = await NuwaWorkflow(self.llm, self.jobs, job_id, payload, target, skill_contact,
            persona, observations, stats, quotes, not send_raw).run()
        self.jobs.progress(job_id, "生成月度趋势与本地索引", .94)
        monthly = []
        for month in sorted(stats["months"]):
            dt = datetime.strptime(month, "%Y-%m").replace(tzinfo=TZ)
            next_month = (dt.replace(day=28) + timedelta(days=4)).replace(day=1)
            month_stats = await asyncio.to_thread(calculate, self.db, cid, max(stats["first_ts"], int(dt.timestamp())),
                                                 min(stats["last_ts"], int(next_month.timestamp()) - 1))
            monthly.append({"period": month, "anchors": month_stats["anchors"], "source": "local_statistics", "sample": month_stats["total"]})
        ranges = []
        for days in (365, 90, 30):
            scoped = recent if days == 30 else await asyncio.to_thread(
                calculate, self.db, cid, max(stats["first_ts"], stats["last_ts"] - days * 86400), stats["last_ts"])
            ranges.append({"period": f"last_{days}", "anchors": scoped["anchors"], "sample": scoped["total"],
                           "source": "local_statistics"})
        timeline = await asyncio.to_thread(timeline_events, self.db, cid, observations)
        write_json(target / "metrics.json", {"monthly": monthly, "ranges": ranges, "timeline": timeline,
                                            "note": "月度与区间曲线为本地统计锚点；近期范围截至该版本最后一条记录。事件日期取证据消息日期。"})
        meta = {"version": version, "source_count": manifest["source_count"], "source_revision": contact["revision"], "sampled": manifest["sampled"], "created_at": now(),
                "model": settings["model"], "thinking": settings["thinking"], "tokens": self.jobs.get(job_id)["tokens"],
                "summary_only": not send_raw, "skill_quality_passed": quality["passed"], "job_id": job_id, "nuwa_status": report["status"]}
        meta.update(distill_mode=distill_mode, coverage_mode=distill_mode,
                    coverage_ratio=manifest["sampled"] / max(1, manifest["total"]),
                    first_covered_messages=manifest["sampled"],
                    first_covered_fragments=sum(manifest.get("fragmented", {}).values()),
                    fragmented_messages=len(manifest.get("fragmented", {})),
                    complete_eligible=False, complete=False)
        completion_error = None
        if full_mode:
            source_current = True
            try:
                current_messages = validate_full_coverage(self.db, cid, target, manifest)
            except AppError as exc:
                if exc.code != "INVALID_FULL_COVERAGE":
                    raise
                source_current = False
                current_messages = canonical_payloads
            canonical = validate_canonical_skill(target, current_messages, quality, report)
            canonical["gates"] = {
                "mode_full": True,
                "coverage_complete": manifest.get("phase4_complete") is True,
                "source_current": source_current,
                **canonical["gates"],
            }
            canonical["complete"] = all(canonical["gates"].values())
            formal_internal = {**manifest, "sources": canonical_messages(self.db, cid)["sources"]}
            formal_parameters = {**parameters, "nuwa_template_version": report.get("method")}
            formal_metadata = {**meta, "contact_id": cid, "session_id": contact["slug"]}
            formal = build_distill_manifest(
                internal=formal_internal,
                parameters=formal_parameters,
                metadata=formal_metadata,
                nuwa_report=report,
                quality=quality,
                canonical=canonical,
                skill_path="skill/SKILL.md",
                built_at=datetime.now(TZ).isoformat(),
            )
            meta.update(complete_eligible=formal["complete"], complete=formal["complete"])
            write_json(target / "distill_manifest.json", formal)
            if not source_current:
                completion_error = AppError(
                    "STALE_DISTILLATION",
                    "正式 Skill 生成期间源消息指纹发生变化；候选诊断已保留但未发布。",
                )
            elif not canonical["gates"]["canonical_files"] or not canonical["gates"]["derived_consistent"]:
                completion_error = AppError(
                    "CANONICAL_SKILL_INCOMPLETE",
                    "canonical Skill 或派生产物不完整；候选诊断已保留但未发布。",
                )
            elif not canonical["gates"]["evidence_valid"]:
                completion_error = AppError(
                    "CANONICAL_EVIDENCE_INVALID",
                    "canonical Skill 引用的 Evidence 不在本次授权便携范围内。",
                )
            elif not canonical["gates"]["nuwa_quality"]:
                completion_error = AppError(
                    "NUWA_QUALITY_INCOMPLETE",
                    "Nuwa 格式、语义状态或 Skill 哈希门禁未通过；候选诊断已保留但未发布。",
                )
        else:
            portable = read_json(target / "skill" / "references" / "evidence.json", {})
            preview_messages = portable.get("evidence_messages", [])
            canonical = validate_canonical_skill(target, preview_messages, quality, report)
            canonical["gates"] = {
                "mode_full": False,
                "coverage_complete": False,
                "source_current": False,
                **canonical["gates"],
            }
            canonical["complete"] = False
            formal_internal = {**manifest, "sources": canonical_messages(self.db, cid)["sources"]}
            formal_parameters = {**parameters, "nuwa_template_version": report.get("method")}
            formal_metadata = {**meta, "contact_id": cid, "session_id": contact["slug"]}
            formal = build_distill_manifest(
                internal=formal_internal,
                parameters=formal_parameters,
                metadata=formal_metadata,
                nuwa_report=report,
                quality=quality,
                canonical=canonical,
                skill_path="skill/SKILL.md",
                built_at=datetime.now(TZ).isoformat(),
            )
            write_json(target / "distill_manifest.json", formal)
        write_json(target / "metadata.json", meta)
        if completion_error:
            raise completion_error
        self.jobs.check(job_id)
        await finish_thread(self.publish, cid, target, meta)
        return {"contact_id": cid, "version": version, "dir": str(root), "quality": quality, "tokens": meta["tokens"], "quotes": len(quotes)}

    def publish(self, cid, target, meta):
        if meta.get("distill_mode") == "sampled" and _active_complete(self.db, cid):
            raise AppError("SAMPLED_CANNOT_REPLACE_COMPLETE",
                           "sampled 预览不能替换已经 complete 的正式人物版本。")
        required = ["persona.json", "radar.json", "quotes.jsonl", "skill/SKILL.md", "metrics.json", "self_style.json"]
        if not all((target / filename).exists() for filename in required):
            raise AppError("INCOMPLETE_ARTIFACTS", "蒸馏产物不完整，未发布。")
        root = target.parent.parent
        for name in ("persona.json", "radar.json", "quotes.jsonl", "metrics.json", "self_style.json", "skill_quality.json", "distill_manifest.json"):
            if not (target / name).exists():
                continue
            atomic_write(root / name, (target / name).read_text(encoding="utf-8"))
        shutil.copytree(target / "skill", root / "skill", dirs_exist_ok=True)
        (root / "rag").mkdir(exist_ok=True)
        write_json(root / "rag" / "index.json", {"storage": "echotrace.db", "embedding": self.config.get()["embedding"], "version": meta["version"]})
        with self.db.connect() as conn:
            rebuild(self.db, self.config, cid, read_quotes(target), connection=conn)
            conn.execute("UPDATE personas SET active=0 WHERE contact_id=?", (cid,))
            conn.execute("INSERT INTO personas(contact_id,version,created_at,model,thinking,token_cost,dir,summary,active) VALUES(?,?,?,?,?,?,?,?,1) ON CONFLICT(contact_id,version) DO UPDATE SET active=1",
                         (cid, meta["version"], meta["created_at"], meta["model"], meta["thinking"], meta["tokens"], str(target), f"{meta['source_count']}条记录；{meta['sampled']}条采样"))
            conn.execute("DELETE FROM metrics WHERE contact_id=?", (cid,))
            for dim in read_json(target / "radar.json")["dimensions"]:
                conn.execute("INSERT INTO metrics(contact_id,period,key,value,confidence,anchor,evidence) VALUES(?,?,?,?,?,?,?)", (cid, "all", dim["key"], dim["score"], dim["confidence"], dim["anchor_score"], dumps(dim["evidence"])))
            for month in read_json(target / "metrics.json")["monthly"]:
                for key, anchor in month["anchors"].items():
                    conn.execute("INSERT INTO metrics(contact_id,period,key,value,confidence,anchor,evidence) VALUES(?,?,?,?,?,?,?)", (cid, month["period"], key, anchor["score"], None, anchor["score"], dumps(anchor["evidence"])))
