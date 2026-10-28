import asyncio
import json
import shutil
import time
import webbrowser
from contextlib import suppress
from pathlib import Path
from urllib.parse import urljoin, urlparse

import httpx
import websockets

from echotrace.config import loopback_url
from echotrace.ingest.cleaner import import_files
from echotrace.util import AppError

RELEASES = "https://github.com/shuakami/qq-chat-exporter/releases"


class QCEClient:
    def __init__(self, db, config, jobs, transport=None):
        self.db, self.config, self.jobs, self.transport = db, config, jobs, transport

    async def request(self, method, path, **kwargs):
        settings = self.config.get(True)["qce"]
        base = loopback_url(settings["base_url"])
        try:
            async with httpx.AsyncClient(timeout=20, trust_env=False, transport=self.transport, follow_redirects=False) as client:
                response = await client.request(method, base + path, headers={"Authorization": "Bearer " + settings["token"]}, **kwargs)
                if response.status_code == 401:
                    raise AppError("QCE_AUTH_FAILED", "QCE Token 无效，请在 QCE 控制台重新复制。")
                if response.status_code == 503:
                    raise AppError("QCE_STANDALONE_MODE", "QCE 当前为独立查看模式或 QQ 未登录，请启动完整登录环境。")
                if response.status_code == 409:
                    raise AppError("TASK_STILL_RUNNING", "QCE 任务仍在运行，请先取消再删除。", 409)
                response.raise_for_status()
                if response.status_code == 204:
                    return {}
                data = response.json()
                if data.get("success") is False:
                    raise AppError("QCE_REQUEST_FAILED", "QCE 未接受请求，请检查 QCE 控制台。")
                return data.get("data", data)
        except httpx.HTTPError as exc:
            raise AppError("QCE_UNREACHABLE", "无法连接本机 QCE。请先安装、启动，再检查地址和 Token。", 502) from exc

    async def detect(self):
        try:
            data = await self.request("GET", "/api/health")
            return {"installed": True, "reachable": True, "version": data.get("version", "未知"),
                    "standalone": data.get("mode") == "standalone", "token_required": False, "details": data}
        except AppError as exc:
            return {"installed": None, "reachable": exc.code in ("QCE_AUTH_FAILED", "QCE_STANDALONE_MODE"), "version": None,
                    "standalone": exc.code == "QCE_STANDALONE_MODE", "token_required": exc.code == "QCE_AUTH_FAILED", "message": exc.message,
                    "note": "未连接不能证明未安装；请确认 QCE 已启动。"}

    async def lookup(self, uin):
        if not str(uin).isdigit():
            raise AppError("INVALID_UIN", "QQ 号必须为数字。")
        data = await self.request("GET", "/api/users/lookup", params={"uin": str(uin)})
        if not data.get("found") or not data.get("uid") or str(data["uid"]).isdigit():
            raise AppError("QCE_UID_NOT_FOUND", "QCE 未找到此 QQ 号的 UID，不能直接用 QQ 号导出。")
        return data

    async def watch(self, task_id, event):
        settings = self.config.get(True)["qce"]
        base = loopback_url(settings["base_url"])
        url = base.replace("http://", "ws://").replace("https://", "wss://") + "/ws"
        try:
            async with websockets.connect(url, additional_headers={"Authorization": "Bearer " + settings["token"]}, open_timeout=4) as ws:
                async for raw in ws:
                    message = json.loads(raw)
                    if message.get("event", message.get("type")) in ("export_progress", "export_complete", "export_error"):
                        if str(message.get("data", {}).get("taskId", task_id)) == str(task_id):
                            event.set()
        except (Exception, asyncio.CancelledError):
            return

    async def cancel_remote(self, job):
        if job["payload"].get("qce_task_id"):
            await self.request("POST", f"/api/tasks/{job['payload']['qce_task_id']}/cancel")

    async def export(self, job_id, payload):
        start, end = int(payload.get("start", 0)), int(payload.get("end", 0))
        if not 0 < start < end < 100_000_000_000:
            raise AppError("INVALID_TIME_RANGE", "请选择有效的起止时间（Unix 秒）。")
        task_id = payload.get("qce_task_id")
        uid = payload.get("peer_uid", "")
        if not task_id:
            if not uid or str(uid).isdigit():
                uid = (await self.lookup(payload.get("uin", uid or "")))["uid"]
            body = {"peer": {"chatType": 1, "peerUid": uid, "guildId": ""}, "sessionName": payload["name"],
                    "format": payload.get("format", "JSON"), "filter": {"startTime": start, "endTime": end},
                    "options": {"includeResourceLinks": bool(payload.get("include_resources", True)), "prettyFormat": True}, "roaming": {"maxMessages": 50000, "maxSequenceQueries": 50000}}
            data = await self.request("POST", "/api/messages/roaming/export", json=body)
            task_id = data.get("taskId", data.get("id"))
            if not task_id:
                raise AppError("QCE_PROTOCOL_ERROR", "QCE 响应缺少任务 ID。")
            self.jobs.update_payload(job_id, qce_task_id=task_id, peer_uid=uid)
        event = asyncio.Event()
        watcher = asyncio.create_task(self.watch(task_id, event))
        try:
            deadline = time.monotonic() + 1800
            while time.monotonic() < deadline:
                self.jobs.check(job_id)
                task = await self.request("GET", f"/api/tasks/{task_id}")
                status = task.get("status")
                if status in ("completed", "succeeded", "complete"):
                    break
                if status in ("failed", "error", "cancelled"):
                    raise AppError("QCE_EXPORT_FAILED", "QCE 导出失败或已取消，请在 QCE 控制台查看原因。")
                progress = task.get("progress", 0)
                if isinstance(progress, dict):
                    progress = progress.get("percentage", 0)
                scanned = task.get("scannedDays", (task.get("progress") or {}).get("scannedDays") if isinstance(task.get("progress"), dict) else None)
                stage = str(task.get("stage") or "QCE 正在导出私聊")
                if scanned is not None:
                    stage += f"；已扫描{scanned}天"
                self.jobs.progress(job_id, stage, min(.8, float(progress) / 100 * .8))
                try:
                    await asyncio.wait_for(event.wait(), timeout=2)
                    event.clear()
                except asyncio.TimeoutError:
                    pass
            else:
                raise AppError("QCE_TIMEOUT", "导出超时，QCE 任务 ID 已保存，可继续查询进度。")
        finally:
            watcher.cancel()
            with suppress(asyncio.CancelledError):
                await watcher
        result = task.get("result") or task
        path = Path(result.get("filePath", ""))
        if not path.is_file():
            download = result.get("downloadUrl")
            if not download:
                raise AppError("QCE_FILE_NOT_FOUND", "QCE 未返回可访问的导出文件，请在控制台检查任务结果。")
            settings = self.config.get(True)["qce"]
            base = loopback_url(settings["base_url"])
            url = urljoin(base + "/", download)
            if (urlparse(url).scheme, urlparse(url).netloc) != (urlparse(base).scheme, urlparse(base).netloc):
                raise AppError("LOCAL_ONLY", "拒绝从外部地址下载聊天记录。")
            ext = {"STREAMING_JSONL": ".jsonl", "TXT": ".txt", "HTML": ".html", "EXCEL": ".xlsx", "STREAMING_ZIP": ".zip"}.get(payload.get("format"), ".json")
            path = self.db.root / "exports" / f"qce-{job_id}{ext}"
            path.parent.mkdir(parents=True, exist_ok=True)
            async with httpx.AsyncClient(timeout=120, trust_env=False, follow_redirects=False, transport=self.transport) as client:
                async with client.stream("GET", url, headers={"Authorization": "Bearer " + settings["token"]}) as response:
                    response.raise_for_status()
                    with path.open("wb") as handle:
                        async for chunk in response.aiter_bytes(1024 * 1024):
                            self.jobs.check(job_id)
                            handle.write(chunk)
        if payload.get("output_dir"):
            destination = Path(payload["output_dir"]).expanduser().resolve()
            destination.mkdir(parents=True, exist_ok=True)
            copied = destination / f"qce-{job_id}{path.suffix}"
            if copied != path.resolve():
                await asyncio.to_thread(shutil.copy2, path, copied)
            path = copied
        if payload.get("format") == "STREAMING_ZIP" or not payload.get("auto_import", True):
            return {"file": str(path), "partial": bool(result.get("partial")), "serverCompletenessProven": False, "note": "文件已导出；ZIP 请解压后导入其中的 JSONL。"}
        self.jobs.progress(job_id, "导出完成，自动导入中", .85)
        source_meta = {"kind": "qce", "task_id": task_id, "partial": bool(result.get("partial")),
                       "server_completeness_proven": False}
        imported = await asyncio.to_thread(import_files, self.db, self.config, [path], payload["name"], payload.get("uin") or uid, payload["me"],
                                          progress=lambda seen, added: self.jobs.progress(job_id, f"自动导入 {seen} 条", .9),
                                          source_meta=source_meta)
        return {**imported, "file": str(path), "partial": bool(result.get("partial")), "serverCompletenessProven": False,
                "note": "无法证明腾讯服务端记录完整，请结合时间跨度和缺失月份检查。"}
