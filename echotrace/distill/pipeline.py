import asyncio
import hashlib
import json
import shutil
import time
from datetime import datetime, timedelta
from pathlib import Path

from echotrace.distill.persona import person, read_quotes
from echotrace.distill.radar import score, timeline_events
from echotrace.distill.skill_writer import write_skill
from echotrace.distill.nuwa import NuwaWorkflow
from echotrace.distill.extraction import extract
from echotrace.distill.evidence import EvidenceIndex
from echotrace.ingest.parsers import TZ
from echotrace.ingest.canonical import canonical_messages
from echotrace.llm.client import estimate_tokens
from echotrace.rag.indexer import from_model, rebuild
from echotrace.stats import calculate
from echotrace.util import AppError, atomic_write, dumps, now, read_json, write_json, finish_thread


def checkpoint_parameters(path, current, message):
    saved = read_json(path)
    if saved is not None:
        # Output allowance can change without invalidating validated results.
        # Keep the original identity so partial chunk fingerprints remain usable.
        saved_identity = {key: value for key, value in saved.items() if key != "max_tokens"}
        current_identity = {key: value for key, value in current.items() if key != "max_tokens"}
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
        manifest = await finish_thread(self.prepare, cid, target, payload, stats, lambda: self.jobs.check(job_id))
        parameters = {"model": settings["model"], "thinking": settings["thinking"], "send_raw": send_raw,
                      "base_url": settings["base_url"], "adapter": settings["adapter"], "thinking_values": settings["thinking_values"],
                      "thinking_field": settings["thinking_field"], "temperature": settings["temperature"], "max_tokens": settings["max_tokens"],
                      "prompts": {name: hashlib.sha256(self.llm.template(name).encode()).hexdigest() for name in
                                  ("distill_chunk", "extract_quotes", "distill_persona", "distill_radar", "distill_skill", "self_style")},
                      "tier": payload.get("tier", "standard")}
        parameters["radar_scales"] = self.config.get()["radar"]["scales"]
        parameters = checkpoint_parameters(target / "parameters.json", parameters,
            "续跑参数与检查点不一致。请恢复原参数，或取消任务后新建蒸馏。")
        completed = 0
        async def process(filename):
            nonlocal completed
            self.jobs.check(job_id)
            messages = read_json(target / "inputs" / filename)
            input_messages = messages if send_raw else summary_messages(messages)
            fingerprint = hashlib.sha256(dumps([input_messages, parameters, self.llm.template("distill_chunk"), self.llm.template("extract_quotes")]).encode()).hexdigest()
            cache = root / "cache" / (fingerprint + ".json")
            checkpoint = target / "chunks" / filename
            result = read_json(checkpoint) or read_json(cache)
            if not result:
                observations = await extract(self.llm, self.jobs, job_id, payload, "distill_chunk",
                    {"target_name": contact["display_name"] if send_raw else "B", "messages": input_messages, "summary_only": not send_raw},
                    target / "chunks" / (filename + ".observations.json"), fingerprint)
                quotes = await extract(self.llm, self.jobs, job_id, payload, "extract_quotes", {"messages": messages, "target": "them"},
                    target / "chunks" / (filename + ".quotes.json"), fingerprint) if send_raw else {"quotes": []}
                result = {"observations": observations["observations"], "quotes": quotes["quotes"]}
                write_json(cache, result)
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
        index = EvidenceIndex(target)
        await finish_thread(index.build, lambda: self.jobs.check(job_id))
        # Preserve the exact packet used by already completed aggregate checkpoints.
        observations = read_json(target / "observations.json")
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
        write_json(target / "metadata.json", meta)
        self.jobs.check(job_id)
        await finish_thread(self.publish, cid, target, meta)
        return {"contact_id": cid, "version": version, "dir": str(root), "quality": quality, "tokens": meta["tokens"], "quotes": len(quotes)}

    def publish(self, cid, target, meta):
        required = ["persona.json", "radar.json", "quotes.jsonl", "skill/SKILL.md", "metrics.json", "self_style.json"]
        if not all((target / filename).exists() for filename in required):
            raise AppError("INCOMPLETE_ARTIFACTS", "蒸馏产物不完整，未发布。")
        root = target.parent.parent
        for name in ("persona.json", "radar.json", "quotes.jsonl", "metrics.json", "self_style.json", "skill_quality.json"):
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
