import asyncio
import difflib
import io
import json
import os
import sqlite3
import uuid
import webbrowser
import zipfile
from pathlib import Path

from fastapi import APIRouter, File, Form, Query, Request, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field

from echotrace.distill.persona import active_dir, artifact, person, read_quotes
from echotrace.distill.skill_writer import FILES, install_skill, quality_check
from echotrace.ingest.cleaner import import_files, preview
from echotrace.ingest.qce_client import RELEASES
from echotrace.llm.client import adapter_parameters
from echotrace.llm.schemas import SCHEMAS
from echotrace.rag.indexer import embed, rebuild, window
from echotrace.rag.retriever import search
from echotrace.stats import calculate
from echotrace.util import AppError, atomic_write, dumps, now, read_json, stable_id, write_json

router = APIRouter(prefix="/api")


def ok(data=None):
    return {"ok": True, "data": {} if data is None else data}


def services(request):
    return request.app.state


class ImportStart(BaseModel):
    upload_ids: list[str] = Field(min_length=1)
    name: str = Field(min_length=1, max_length=80)
    uid: str = Field(default="", max_length=200)
    me: list[str] = Field(min_length=1)
    pattern: str | None = None


class DistillStart(BaseModel):
    consent: bool = False
    send_raw: bool = True
    model: str | None = None
    thinking: str | None = Field(default=None, pattern="^(low|medium|high|max)$")
    tier: str = Field(default="standard", pattern="^(quick|standard|deep)$")
    scope: dict = {}
    token_limit: int = Field(default=500000, ge=10000, le=100000000)


class AdviceInput(BaseModel):
    consent: bool = False
    draft: str = Field(default="", max_length=8000)
    recent_text: str = Field(default="", max_length=20000)
    recent_n: int | None = Field(default=None, ge=20, le=200)
    regenerate_strategy: str | None = Field(default=None, pattern="^(safe|warm|honest)$")


def upload_paths(state, upload_ids):
    paths = []
    for upload_id in upload_ids:
        if not all(c in "0123456789abcdef" for c in upload_id) or len(upload_id) != 32:
            raise AppError("INVALID_UPLOAD", "上传标识无效。")
        matches = list((state.db.root / "exports" / "uploads").glob(upload_id + ".*"))
        if len(matches) != 1:
            raise AppError("INVALID_UPLOAD", "上传文件不存在，请重新选择。")
        paths.append(str(matches[0]))
    return paths


async def save_uploads(state, files):
    root = state.db.root / "exports" / "uploads"
    root.mkdir(parents=True, exist_ok=True)
    ids, paths = [], []
    for file in files:
        suffix = Path(file.filename or "").suffix.lower()
        if suffix not in {".json", ".jsonl", ".ndjson", ".txt", ".html", ".htm", ".xlsx", ".xlsm"}:
            raise AppError("IMPORT_PARSE_FAILED", "请选择 JSON / JSONL / TXT / HTML / XLSX 文件。")
        upload_id = uuid.uuid4().hex
        path = root / (upload_id + suffix)
        try:
            with path.open("wb") as handle:
                while chunk := await file.read(1024 * 1024):
                    await asyncio.to_thread(handle.write, chunk)
        finally:
            await file.close()
        ids.append(upload_id)
        paths.append(str(path))
    return ids, paths


@router.get("/health")
async def health(request: Request):
    s = services(request)
    return ok({"status": "ok", "version": "0.1.0", "llm": {"configured": bool(s.config.get()["llm"]["base_url"]), "probed": False},
               "qce": {"probed": False}, "vectors": {"sqlite_vec": s.db.vector_available, "provider": s.config.get()["embedding"]["provider"]},
               "local_only": True})


@router.get("/settings")
async def settings(request: Request):
    s = services(request)
    return ok({**s.config.get(), "actual_parameters": adapter_parameters(s.config.get()["llm"])})


@router.put("/settings")
async def settings_update(request: Request, payload: dict):
    s = services(request)
    old_settings = s.config.get(True)
    previous = old_settings["embedding"]
    if "embedding" in payload and s.db.one("SELECT id FROM jobs WHERE kind='distill' AND status IN ('queued','running','paused')"):
        raise AppError("JOB_ALREADY_RUNNING", "请先完成或取消蒸馏，再更换嵌入配置。")
    result = s.config.update(payload)
    if previous != result["embedding"]:
        def rebuild_all():
            embed("本地模型可用性检查", s.config)
            with s.db.connect() as conn:
                for row in s.db.rows("SELECT contact_id,dir FROM personas WHERE active=1"):
                    rebuild(s.db, s.config, row["contact_id"], read_quotes(Path(row["dir"])), connection=conn)
        try:
            await asyncio.to_thread(rebuild_all)
        except BaseException:
            s.config.update(old_settings)
            raise
    return ok({**result, "actual_parameters": adapter_parameters(result["llm"])})


@router.post("/settings/test-llm")
async def test_llm(request: Request):
    return ok(await services(request).llm.test())


@router.post("/settings/test-qce")
@router.get("/qce/detect")
async def detect_qce(request: Request):
    return ok(await services(request).qce.detect())


@router.post("/qce/open-releases")
async def releases():
    webbrowser.open(RELEASES)
    return ok({"url": RELEASES})


@router.get("/qce/sessions")
async def qce_sessions(request: Request):
    data = await services(request).qce.request("GET", "/api/sessions")
    return ok(data)


@router.get("/qce/tasks")
async def qce_tasks(request: Request):
    return ok(await services(request).qce.request("GET", "/api/tasks"))


@router.post("/qce/tasks/{task_id}/cancel")
async def qce_task_cancel(request: Request, task_id: str):
    return ok(await services(request).qce.request("POST", f"/api/tasks/{task_id}/cancel"))


@router.delete("/qce/tasks/{task_id}")
async def qce_task_delete(request: Request, task_id: str):
    return ok(await services(request).qce.request("DELETE", f"/api/tasks/{task_id}"))


@router.get("/qce/lookup")
async def lookup(request: Request, uin: str):
    return ok(await services(request).qce.lookup(uin))


@router.post("/qce/export")
async def export_qce(request: Request, payload: dict):
    if not payload.get("me") or not payload.get("name"):
        raise AppError("IDENTITY_REQUIRED", "请填写称呼和「我」的 QQ 号或昵称。")
    if payload.get("format", "JSON") not in ("JSON", "STREAMING_JSONL", "TXT", "HTML", "EXCEL", "STREAMING_ZIP"):
        raise AppError("INVALID_FORMAT", "请选择受支持的导出格式。")
    return ok(services(request).jobs.create("qce_export", payload))


@router.get("/backups")
async def backups(request: Request):
    return ok(services(request).db.rows("SELECT * FROM backups ORDER BY next_run"))


@router.post("/backups")
async def backup_create(request: Request, payload: dict):
    interval = int(payload.get("interval_hours", 24))
    if interval < 1 or not payload.get("me") or not payload.get("name"):
        raise AppError("INVALID_BACKUP", "备份间隔至少1小时，且需填写人物与身份。")
    bid = uuid.uuid4().hex
    services(request).db.execute("INSERT INTO backups VALUES(?,?,?,?,?,1)", (bid, payload.get("contact_id"), dumps(payload), interval, now() + interval * 3600))
    return ok({"id": bid})


@router.delete("/backups/{bid}")
async def backup_delete(request: Request, bid: str):
    services(request).db.execute("DELETE FROM backups WHERE id=?", (bid,))
    return ok()


@router.post("/import/preview")
async def import_preview(request: Request, files: list[UploadFile] = File(...), pattern: str | None = Form(None)):
    s = services(request)
    ids, paths = await save_uploads(s, files)
    result = await asyncio.to_thread(preview, paths, pattern)
    return ok({**result, "upload_ids": ids})


@router.post("/import/paste")
async def import_paste(request: Request, payload: dict):
    text = payload.get("text", "")
    if len(text) > 2000000:
        raise AppError("IMPORT_TOO_LARGE", "粘贴内容过大，请改用文件导入。")
    s = services(request)
    upload_id = uuid.uuid4().hex
    path = s.db.root / "exports" / "uploads" / (upload_id + ".txt")
    atomic_write(path, text)
    return ok({**await asyncio.to_thread(preview, [path], payload.get("pattern")), "upload_ids": [upload_id]})


@router.post("/import/start")
async def import_start(request: Request, payload: ImportStart):
    s = services(request)
    paths = upload_paths(s, payload.upload_ids)
    return ok(s.jobs.create("import", {**payload.model_dump(), "paths": paths}))


@router.post("/import/file")
async def import_file(request: Request, files: list[UploadFile] = File(...), name: str = Form(...), me: str = Form(...), uid: str = Form("")):
    s = services(request)
    ids, paths = await save_uploads(s, files)
    return ok(s.jobs.create("import", {"paths": paths, "name": name, "uid": uid, "me": [x.strip() for x in me.split(",") if x.strip()]}))


@router.get("/contacts")
async def contacts(request: Request):
    return ok(services(request).db.rows("SELECT c.*,s.msg_count,s.first_ts,s.last_ts,p.version,p.summary FROM contacts c LEFT JOIN sessions s ON s.contact_id=c.id LEFT JOIN personas p ON p.contact_id=c.id AND p.active=1 ORDER BY s.last_ts DESC"))


@router.get("/contacts/{cid}")
async def contact_detail(request: Request, cid: int):
    s = services(request)
    contact = person(s.db, cid)
    report = read_json(s.db.root / "people" / contact["slug"] / "health.json")
    if report is None:
        report = await asyncio.to_thread(calculate, s.db, cid)
    version = s.db.one("SELECT * FROM personas WHERE contact_id=? AND active=1", (cid,))
    metadata = read_json(Path(version["dir"]) / "metadata.json", {}) if version else {}
    return ok({**contact, "stats": report, "version": version["version"] if version else None,
               "needs_update": bool(version and (metadata.get("source_count", 0) != report["total"] or metadata.get("source_revision", 0) != contact["revision"])), "metadata": metadata})


@router.get("/contacts/{cid}/messages")
async def messages(request: Request, cid: int, before_ts: int | None = None, before_id: int | None = None, limit: int = Query(50, ge=1, le=200), q: str = "", sender: str = ""):
    s = services(request)
    conditions, args = ["s.contact_id=?"], [cid]
    if before_ts is not None:
        conditions.append("(m.ts<? OR (m.ts=? AND m.id<?))")
        args += [before_ts, before_ts, before_id or 0]
    if q:
        conditions.append("m.text LIKE ?")
        args.append("%" + q + "%")
    if sender:
        conditions.append("m.sender=?")
        args.append(sender)
    rows = s.db.rows("SELECT m.id,m.msg_id,m.ts,m.sender,m.sender_raw,m.kind,m.text,m.reply_to,m.flags FROM messages m JOIN sessions s ON s.id=m.session_id WHERE " + " AND ".join(conditions) + " ORDER BY m.ts DESC,m.id DESC LIMIT ?", (*args, limit))
    for row in rows:
        row["flags"] = json.loads(row["flags"])
    return ok({"messages": rows[::-1], "next": {"before_ts": rows[-1]["ts"], "before_id": rows[-1]["id"]} if len(rows) == limit else None})


@router.get("/contacts/{cid}/messages/{mid}")
async def message_context(request: Request, cid: int, mid: int):
    s = services(request)
    context = window(s.db, cid, mid)
    row = s.db.one("SELECT * FROM messages WHERE id=?", (mid,))
    row["raw"] = json.loads(row["raw"])
    row["flags"] = json.loads(row["flags"])
    return ok({"message": row, "context": context})


@router.post("/contacts/{cid}/speakers")
async def correct_speakers(request: Request, cid: int, payload: dict):
    s = services(request)
    mapping = payload.get("mapping", {})
    if not mapping or any(v not in ("me", "them", "system") for v in mapping.values()):
        raise AppError("INVALID_MAPPING", "请提供昵称或身份到 me/them/system 的映射。")
    if s.db.one("SELECT id FROM jobs WHERE kind='distill' AND status IN ('queued','running','paused') AND json_extract(payload,'$.contact_id')=?", (cid,)):
        raise AppError("JOB_ALREADY_RUNNING", "请先结束蒸馏任务，再修正发言人。")
    with s.db.connect() as conn:
        for identity, role in mapping.items():
            conn.execute("INSERT OR REPLACE INTO speaker_mappings(session_id,identity,role) SELECT id,?,? FROM sessions WHERE contact_id=?", (identity, role, cid))
            conn.execute("UPDATE messages SET sender=? WHERE session_id IN (SELECT id FROM sessions WHERE contact_id=?) AND (sender_raw=? OR json_extract(flags,'$.identity')=?)", (role, cid, identity, identity))
        conn.execute("UPDATE contacts SET revision=revision+1 WHERE id=?", (cid,))
    report = await asyncio.to_thread(calculate, s.db, cid)
    p = person(s.db, cid)
    write_json(s.db.root / "people" / p["slug"] / "health.json", report)
    s.db.execute("UPDATE personas SET summary=summary || '；发言人已修正，请重新蒸馏' WHERE contact_id=? AND active=1", (cid,))
    return ok(report)


@router.get("/contacts/{cid}/export")
async def export_contact(request: Request, cid: int, format: str = "jsonl"):
    db = services(request).db
    person(db, cid)
    def generate():
        with db.connect() as conn:
            for row in conn.execute("SELECT m.* FROM messages m JOIN sessions s ON s.id=m.session_id WHERE s.contact_id=? ORDER BY ts,m.id", (cid,)):
                yield (dumps(dict(row)) + "\n") if format == "jsonl" else f"{row['ts']} {row['sender_raw']}: {row['text']}\n"
    return StreamingResponse(generate(), media_type="application/x-ndjson" if format == "jsonl" else "text/plain", headers={"Content-Disposition": f'attachment; filename="contact-{cid}.{format}"'})


@router.get("/contacts/{cid}/distill/estimate")
async def estimate(request: Request, cid: int, tier: str = "standard"):
    return ok(services(request).pipeline.estimate(cid, tier))


@router.post("/contacts/{cid}/distill")
async def distill(request: Request, cid: int, payload: DistillStart):
    s = services(request)
    person(s.db, cid)
    count = s.db.one("SELECT msg_count FROM sessions WHERE contact_id=?", (cid,))
    if not count or count["msg_count"] < 200:
        raise AppError("NOT_ENOUGH_DATA", "消息少于200条，请先补充记录。")
    if not payload.consent:
        raise AppError("CONSENT_REQUIRED", "开始前需明确同意本次向所选模型发送上下文。")
    data = payload.model_dump(exclude_none=True)
    s.llm.settings(data)
    return ok(s.jobs.create("distill", {**data, "contact_id": cid}))


@router.get("/contacts/{cid}/persona")
async def persona(request: Request, cid: int, version: int | None = None):
    return ok(artifact(services(request).db, cid, "persona.json", version))


@router.get("/contacts/{cid}/radar")
async def radar(request: Request, cid: int, period: str = "all", version: int | None = None):
    s = services(request)
    data = artifact(s.db, cid, "radar.json", version)
    metrics = artifact(s.db, cid, "metrics.json", version)
    return ok({**data, "metrics": metrics, "selected_period": period})


@router.get("/contacts/{cid}/skill")
async def skill_get(request: Request, cid: int):
    root = active_dir(services(request).db, cid)
    return ok({"markdown": (root / "skill" / "SKILL.md").read_text(encoding="utf-8"),
               "research": [{"name": name, "content": (root / "skill" / "references" / "research" / name).read_text(encoding="utf-8")} for name in FILES],
               "quality": read_json(root / "skill_quality.json")})


@router.get("/contacts/{cid}/skill/download")
async def skill_download(request: Request, cid: int):
    root = active_dir(services(request).db, cid) / "skill"
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for file in root.rglob("*"):
            if file.is_file():
                archive.write(file, str(file.relative_to(root)))
    return StreamingResponse(iter([buffer.getvalue()]), media_type="application/zip", headers={"Content-Disposition": f'attachment; filename="skill-{cid}.zip"'})


@router.post("/contacts/{cid}/skill/install")
async def skill_install(request: Request, cid: int, payload: dict):
    s = services(request)
    if payload.get("destination") not in ("agents", "project"):
        raise AppError("INVALID_DESTINATION", "请选择用户 Skill 目录或当前项目目录。")
    return ok(await asyncio.to_thread(install_skill, active_dir(s.db, cid) / "skill", person(s.db, cid)["slug"], payload["destination"]))


@router.post("/contacts/{cid}/skill/quality-check")
async def check_skill(request: Request, cid: int):
    root = active_dir(services(request).db, cid)
    result = await asyncio.to_thread(quality_check, root / "skill" / "SKILL.md")
    write_json(root / "skill_quality.json", result)
    return ok(result)


@router.post("/contacts/{cid}/skill/regenerate")
async def regenerate_skill(request: Request, cid: int):
    s = services(request)
    root = active_dir(s.db, cid)
    return ok(s.jobs.create("distill", {"contact_id": cid, "skill_only": True, "local_only": True,
                                       "base_version": read_json(root / "metadata.json")["version"]}))


@router.post("/contacts/{cid}/skill/rerun")
async def rerun_skill(request: Request, cid: int, payload: DistillStart):
    s = services(request)
    row = s.db.one("SELECT version FROM personas WHERE contact_id=? AND active=1", (cid,))
    if not row:
        raise AppError("NOT_DISTILLED", "请先完成一次蒸馏。")
    if not payload.consent:
        raise AppError("CONSENT_REQUIRED", "重跑前需明确同意向所选模型发送上下文。")
    s.llm.settings(payload.model_dump(exclude_none=True))
    return ok(s.jobs.create("distill", {**payload.model_dump(exclude_none=True), "contact_id": cid, "skill_only": True, "base_version": row["version"]}))


@router.get("/contacts/{cid}/quotes")
async def quotes(request: Request, cid: int, q: str = "", topic: str | None = None, limit: int = Query(50, ge=1, le=200)):
    s = services(request)
    return ok(await asyncio.to_thread(search, s.db, s.config, cid, q, limit, topic=topic))


@router.post("/contacts/{cid}/quotes")
async def quote_add(request: Request, cid: int, payload: dict):
    s = services(request)
    mid = int(payload["message_id"])
    context = window(s.db, cid, mid)
    message = next(m for m in context if m["id"] == mid)
    qid = stable_id(f"pick:{cid}:{mid}")
    q = {"id": qid, "contact_id": cid, "people_id": cid, "text": message["text"], "start_msg": context[0]["id"], "end_msg": context[-1]["id"],
         "evidence": [mid], "score": 10, "dims": {"reuse": 10, "emotion": 10, "relation": 10, "info": 10}, "source": "user_pick",
         "ts": message["ts"], "speaker": message["sender"], "topic": payload.get("topic", "手动收藏"), "sensitive": False}
    existing = s.db.rows("SELECT * FROM quotes WHERE contact_id=?", (cid,))
    for row in existing:
        for key in ("dims", "evidence"):
            row[key] = json.loads(row[key])
    await asyncio.to_thread(rebuild, s.db, s.config, cid, existing + [q])
    return ok(q)


@router.delete("/contacts/{cid}/quotes/{qid}")
async def quote_delete(request: Request, cid: int, qid: int):
    s = services(request)
    with s.db.connect() as conn:
        if conn.execute("SELECT id FROM quotes WHERE contact_id=? AND id=?", (cid, qid)).fetchone():
            conn.execute("DELETE FROM quotes WHERE id=?", (qid,))
            conn.execute("DELETE FROM quotes_fts WHERE rowid=?", (qid,))
            if s.db.vector_available:
                s.db.load_vec(conn)
                conn.execute("DELETE FROM quote_vectors WHERE id=?", (qid,))
    return ok()


@router.get("/contacts/{cid}/quotes/export")
async def quotes_export(request: Request, cid: int, format: str = "jsonl"):
    rows = services(request).db.rows("SELECT id,text,evidence,ts,score,topic,source FROM quotes WHERE contact_id=? ORDER BY ts", (cid,))
    for row in rows:
        row["evidence"] = json.loads(row["evidence"])
    text = "".join(dumps(r) + "\n" for r in rows) if format == "jsonl" else "# 重要话语\n\n" + "\n\n".join(f"> {r['text']}\n\nevidence: {r['evidence']}" for r in rows)
    return StreamingResponse(iter([text]), media_type="text/plain", headers={"Content-Disposition": f'attachment; filename="quotes-{cid}.{format}"'})


@router.post("/contacts/{cid}/advisor")
async def advise(request: Request, cid: int, payload: AdviceInput):
    return ok(await services(request).advisor.advise(cid, payload.model_dump(exclude_none=True)))


@router.post("/contacts/{cid}/chat")
async def chat(request: Request, cid: int, payload: AdviceInput):
    s = services(request)
    payload = payload.model_dump(exclude_none=True)
    # Validate consent and safety before SSE headers so errors retain the uniform envelope.
    s.advisor.context(cid, payload)
    async def events():
        task = asyncio.create_task(s.advisor.chat(cid, payload))
        try:
            while not task.done():
                yield ": waiting\n\n"
                await asyncio.wait({task}, timeout=3)
            result = await task
            for i in range(0, len(result["text"]), 12):
                yield "event: delta\ndata: " + dumps({"text": result["text"][i:i + 12]}) + "\n\n"
            yield "event: done\ndata: " + dumps(result) + "\n\n"
        except AppError as exc:
            yield "event: error\ndata: " + dumps({"code": exc.code, "message": exc.message}) + "\n\n"
        finally:
            if not task.done():
                task.cancel()
    return StreamingResponse(events(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})


@router.get("/contacts/{cid}/chat")
async def chat_history(request: Request, cid: int):
    return ok(services(request).db.rows("SELECT * FROM conversations WHERE contact_id=? ORDER BY id", (cid,)))


@router.post("/contacts/{cid}/actions")
async def action(request: Request, cid: int, payload: dict):
    if payload.get("action") not in ("adopt", "favorite", "draft"):
        raise AppError("INVALID_ACTION", "操作无效。")
    services(request).db.execute("INSERT INTO local_actions(contact_id,action,payload,created_at) VALUES(?,?,?,?)", (cid, payload["action"], dumps(payload.get("option", {})), now()))
    return ok()


@router.get("/contacts/{cid}/actions")
async def action_list(request: Request, cid: int):
    rows = services(request).db.rows("SELECT * FROM local_actions WHERE contact_id=? AND action='favorite' ORDER BY id DESC LIMIT 100", (cid,))
    for row in rows:
        row["payload"] = json.loads(row["payload"])
    return ok(rows)


@router.get("/contacts/{cid}/versions")
async def versions(request: Request, cid: int):
    return ok(services(request).db.rows("SELECT * FROM personas WHERE contact_id=? ORDER BY version DESC", (cid,)))


@router.get("/contacts/{cid}/versions/diff")
async def version_diff(request: Request, cid: int, a: int, b: int):
    db = services(request).db
    pa, pb = artifact(db, cid, "persona.json", a), artifact(db, cid, "persona.json", b)
    ra, rb = artifact(db, cid, "radar.json", a), artifact(db, cid, "radar.json", b)
    return ok({"persona_diff": "\n".join(difflib.unified_diff(json.dumps(pa, ensure_ascii=False, indent=2).splitlines(), json.dumps(pb, ensure_ascii=False, indent=2).splitlines(), fromfile=f"v{a}", tofile=f"v{b}", lineterm="")),
               "radar_changes": [{"key": x["key"], "label": x["label"], "before": x["score"], "after": next(y["score"] for y in rb["dimensions"] if y["key"] == x["key"])} for x in ra["dimensions"]]})


@router.post("/contacts/{cid}/versions/rollback")
async def rollback(request: Request, cid: int, payload: dict):
    s = services(request)
    if s.db.one("SELECT id FROM jobs WHERE kind='distill' AND status IN ('queued','running','paused') AND json_extract(payload,'$.contact_id')=?", (cid,)):
        raise AppError("JOB_ALREADY_RUNNING", "请先取消该人物的蒸馏任务再回滚。")
    row = s.db.one("SELECT dir FROM personas WHERE contact_id=? AND version=?", (cid, payload.get("version")))
    if not row:
        raise AppError("NOT_FOUND", "目标版本不存在。", 404)
    root = Path(row["dir"])
    await asyncio.to_thread(s.pipeline.publish, cid, root, read_json(root / "metadata.json"))
    return ok({"version": payload["version"]})


@router.get("/jobs")
async def jobs_list(request: Request):
    s = services(request)
    return ok([s.jobs.get(row["id"]) for row in s.db.rows("SELECT id FROM jobs ORDER BY created_at DESC LIMIT 100")])


@router.get("/jobs/{job_id}")
async def job_get(request: Request, job_id: str):
    return ok(services(request).jobs.get(job_id))


def job_file(s, job_id):
    job = s.jobs.get(job_id)
    path = Path((job.get("result") or {}).get("file", ""))
    if job["kind"] != "qce_export" or job["status"] != "succeeded" or not path.is_file():
        raise AppError("NOT_FOUND", "任务尚无可访问的导出文件。", 404)
    return job, path.resolve()


@router.get("/jobs/{job_id}/download")
async def job_download(request: Request, job_id: str):
    _, path = job_file(services(request), job_id)
    return FileResponse(path, filename=path.name)


@router.post("/jobs/{job_id}/open-folder")
async def job_open_folder(request: Request, job_id: str):
    _, path = job_file(services(request), job_id)
    if os.name == "nt":
        os.startfile(str(path.parent))
    else:
        webbrowser.open(path.parent.as_uri())
    return ok({"path": str(path.parent)})


@router.post("/jobs/{job_id}/import")
async def job_import(request: Request, job_id: str):
    s = services(request)
    job, path = job_file(s, job_id)
    if path.suffix.lower() == ".zip":
        raise AppError("IMPORT_PARSE_FAILED", "请先手动解压 ZIP，再导入其中的 JSONL 文件。")
    payload = job["payload"]
    return ok(s.jobs.create("import", {"paths": [str(path)], "name": payload["name"], "uid": payload.get("uin") or payload.get("peer_uid", ""), "me": payload["me"]}))


@router.post("/jobs/{job_id}/cancel")
async def job_cancel(request: Request, job_id: str):
    s = services(request)
    job = s.jobs.get(job_id)
    if job["kind"] == "qce_export" and job["payload"].get("qce_task_id"):
        try:
            await s.qce.cancel_remote(job)
        except AppError:
            # A disconnected exporter must not prevent stopping local reads/imports.
            pass
    return ok(await s.jobs.cancel(job_id))


@router.post("/jobs/{job_id}/pause")
async def job_pause(request: Request, job_id: str):
    return ok(await services(request).jobs.cancel(job_id, pause=True))


@router.post("/jobs/{job_id}/resume")
async def job_resume(request: Request, job_id: str, payload: dict):
    return ok(services(request).jobs.resume(job_id, payload.get("extra_budget", 0)))


@router.get("/jobs/{job_id}/events")
async def job_events(request: Request, job_id: str):
    s = services(request)
    s.jobs.get(job_id)
    async def generate():
        while not await request.is_disconnected():
            job = s.jobs.get(job_id)
            yield "data: " + dumps(job) + "\n\n"
            if job["status"] not in ("running", "queued"):
                break
            await asyncio.sleep(1)
    return StreamingResponse(generate(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})


@router.get("/prompts")
async def prompts(request: Request):
    return ok([{"name": key, "content": services(request).llm.template(key)} for key in SCHEMAS])


@router.put("/prompts/{name}")
async def prompt_save(request: Request, name: str, payload: dict):
    s = services(request)
    if name not in SCHEMAS or not isinstance(payload.get("content"), str):
        raise AppError("INVALID_PROMPT", "提示词名称或内容无效。")
    if "{{context}}" not in payload["content"] or "{{schema}}" not in payload["content"]:
        raise AppError("INVALID_PROMPT", "必须保留 {{context}} 与 {{schema}} 两个变量。")
    s.db.execute("INSERT INTO prompt_versions(name,content,created_at) VALUES(?,?,?)", (name, s.llm.template(name), now()))
    atomic_write(s.config.prompt_dir() / f"{name}.md", payload["content"])
    return ok()


@router.get("/about")
async def about():
    path = Path(__file__).parents[2] / "NOTICE.md"
    return ok({"notice": path.read_text(encoding="utf-8") if path.exists() else "参考 qq-chat-exporter（GPL-3.0）、女娲和 NapCat；通过公开协议互操作，不分发上游程序。", "version": "0.1.0"})
