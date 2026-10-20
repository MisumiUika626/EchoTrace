import asyncio
import json
import sqlite3
import uuid
from contextlib import suppress

from echotrace.util import AppError, dumps, now


class JobStopped(Exception):
    pass


class JobManager:
    def __init__(self, db):
        self.db, self.handlers, self.tasks = db, {}, {}

    def get(self, job_id):
        row = self.db.one("SELECT * FROM jobs WHERE id=?", (job_id,))
        if not row:
            raise AppError("NOT_FOUND", "任务不存在。", 404)
        for k in ("payload", "result"):
            row[k] = json.loads(row[k]) if row[k] else None
        row["tokens"] = self.db.one("SELECT COALESCE(SUM(total_tokens),0) AS n FROM token_usage WHERE job_id=?", (job_id,))["n"]
        return row

    def check(self, job_id):
        state = self.db.one("SELECT status FROM jobs WHERE id=?", (job_id,))
        if not state or state["status"] in ("cancelled", "paused"):
            raise JobStopped()

    def progress(self, job_id, stage, progress, **extra):
        self.check(job_id)
        self.db.execute("UPDATE jobs SET stage=?,progress=?,updated_at=? WHERE id=?", (stage, max(0, min(1, progress)), now(), job_id))
        if extra:
            result = self.get(job_id)["result"] or {}
            result.update(extra)
            self.db.execute("UPDATE jobs SET result=? WHERE id=?", (dumps(result), job_id))

    def update_payload(self, job_id, **fields):
        payload = self.get(job_id)["payload"]
        payload.update(fields)
        self.db.execute("UPDATE jobs SET payload=?,updated_at=? WHERE id=?", (dumps(payload), now(), job_id))

    def create(self, kind, payload):
        job_id = uuid.uuid4().hex
        try:
            self.db.execute("INSERT INTO jobs VALUES(?,?,?,0,?,?,NULL,NULL,?,?)", (job_id, kind, "queued", "等待执行", dumps(payload), now(), now()))
        except sqlite3.IntegrityError as exc:
            raise AppError("JOB_ALREADY_RUNNING", "此人物已有蒸馏任务，请先恢复、取消或等待完成。", 409) from exc
        self.tasks[job_id] = asyncio.create_task(self.run(job_id))
        return self.get(job_id)

    async def run(self, job_id):
        try:
            self.check(job_id)
            job = self.get(job_id)
            self.db.execute("UPDATE jobs SET status='running',error=NULL,updated_at=? WHERE id=?", (now(), job_id))
            result = await self.handlers[job["kind"]](job_id, job["payload"])
            self.check(job_id)
            self.db.execute("UPDATE jobs SET status='succeeded',stage='已完成',progress=1,result=?,updated_at=? WHERE id=?", (dumps(result), now(), job_id))
        except (JobStopped, asyncio.CancelledError):
            if self.get(job_id)["status"] not in ("cancelled", "paused"):
                self.db.execute("UPDATE jobs SET status='paused',stage='任务已暂停，可继续',updated_at=? WHERE id=?", (now(), job_id))
        except Exception as exc:
            error = exc.message if isinstance(exc, AppError) else f"任务执行失败（{type(exc).__name__}），已保留检查点。"
            self.db.execute("UPDATE jobs SET status='failed',error=?,stage='等待修复后重试',updated_at=? WHERE id=?", (error, now(), job_id))
        finally:
            self.tasks.pop(job_id, None)

    def resume(self, job_id, extra_budget=0):
        job = self.get(job_id)
        if job_id in self.tasks:
            raise AppError("JOB_STOPPING", "任务正在保存并停止，请稍后点击继续。", 409)
        if job["status"] not in ("paused", "failed", "cancelled"):
            raise AppError("INVALID_JOB_STATE", "只有暂停、失败或取消的任务可以继续。")
        if extra_budget:
            self.update_payload(job_id, token_limit=job["payload"].get("token_limit", 200000) + max(0, int(extra_budget)))
        pending = job["payload"].get("nuwa_pending")
        if pending:
            approved = job["payload"].get("nuwa_approved", [])
            self.update_payload(job_id, nuwa_approved=list(dict.fromkeys(approved + [pending])), nuwa_pending=None)
        try:
            self.db.execute("UPDATE jobs SET status='queued',error=NULL,stage='等待继续',updated_at=? WHERE id=?", (now(), job_id))
        except sqlite3.IntegrityError as exc:
            raise AppError("JOB_ALREADY_RUNNING", "此人物另有蒸馏任务正在执行。", 409) from exc
        if job_id not in self.tasks:
            self.tasks[job_id] = asyncio.create_task(self.run(job_id))
        return self.get(job_id)

    async def cancel(self, job_id, pause=False):
        job = self.get(job_id)
        if job["status"] == "succeeded":
            raise AppError("INVALID_JOB_STATE", "已完成的任务无需取消。")
        state = "paused" if pause else "cancelled"
        self.db.execute("UPDATE jobs SET status=?,stage=?,updated_at=? WHERE id=?", (state, "已暂停" if pause else "已取消", now(), job_id))
        task = self.tasks.get(job_id)
        if task:
            # A worker thread must observe the persisted stop flag before it is resumed.
            if job["kind"] not in ("import", "qce_export"):
                task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        return self.get(job_id)

    async def startup(self):
        # Restart never silently sends chat text to an external provider.
        self.db.execute("UPDATE jobs SET status='paused',stage='上次运行中断，请点击继续',updated_at=? WHERE status IN ('running','queued')", (now(),))

    async def shutdown(self):
        for job_id in list(self.tasks):
            await self.cancel(job_id, pause=True)
        if self.tasks:
            await asyncio.gather(*list(self.tasks.values()), return_exceptions=True)
