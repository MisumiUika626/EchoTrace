import asyncio
import json
import os
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from urllib.parse import urlparse

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from pydantic import ValidationError
from starlette.exceptions import HTTPException
from starlette.staticfiles import StaticFiles

from echotrace.advisor.advise import Advisor
from echotrace.api.routes import router
from echotrace.config import Config
from echotrace.db import Database
from echotrace.distill.pipeline import Pipeline
from echotrace.ingest.cleaner import import_files
from echotrace.ingest.qce_client import QCEClient
from echotrace.jobs import JobManager
from echotrace.llm.client import LLMClient
from echotrace.util import AppError, now


def create_app(data_dir=None, llm_transport=None, qce_transport=None):
    db = Database(Path(data_dir or os.environ.get("ECHOTRACE_DATA_DIR", "data")))
    config = Config(db)
    jobs = JobManager(db)
    llm = LLMClient(config, jobs, llm_transport)
    pipeline = Pipeline(db, config, jobs, llm)
    qce = QCEClient(db, config, jobs, qce_transport)

    async def run_import(job_id, payload):
        return await asyncio.to_thread(import_files, db, config, payload["paths"], payload["name"], payload.get("uid", ""), payload["me"], payload.get("pattern"),
                                       lambda read, added: jobs.progress(job_id, f"已解析 {read:,} 条，新增 {added:,} 条", min(.9, read / (read + 2000))))
    jobs.handlers = {"import": run_import, "distill": pipeline.run, "qce_export": qce.export}

    async def backups_loop():
        while True:
            for row in db.rows("SELECT * FROM backups WHERE enabled=1 AND next_run<=?", (now(),)):
                payload = json.loads(row["payload"])
                payload["end"] = now()
                jobs.create("qce_export", payload)
                db.execute("UPDATE backups SET next_run=? WHERE id=?", (now() + row["interval_hours"] * 3600, row["id"]))
            await asyncio.sleep(30)

    @asynccontextmanager
    async def lifespan(app):
        await jobs.startup()
        scheduler = asyncio.create_task(backups_loop())
        yield
        scheduler.cancel()
        with suppress(asyncio.CancelledError):
            await scheduler
        await jobs.shutdown()

    app = FastAPI(title="EchoTrace", version="0.1.0", lifespan=lifespan, docs_url="/api/docs", redoc_url=None)
    app.state.db, app.state.config, app.state.jobs = db, config, jobs
    app.state.llm, app.state.pipeline, app.state.qce = llm, pipeline, qce
    app.state.advisor = Advisor(db, config, llm)

    @app.middleware("http")
    async def local_only(request: Request, call_next):
        host = request.url.hostname
        if host not in ("localhost", "127.0.0.1", "::1", "testserver"):
            return JSONResponse({"ok": False, "error": {"code": "LOCAL_ONLY", "message": "仅允许本机访问。"}}, status_code=403)
        origin = request.headers.get("origin")
        if origin:
            parsed = urlparse(origin)
            if parsed.hostname not in ("localhost", "127.0.0.1", "::1", "testserver") or parsed.scheme not in ("http", "https"):
                return JSONResponse({"ok": False, "error": {"code": "INVALID_ORIGIN", "message": "拒绝外部网页访问本地数据。"}}, status_code=403)
            if parsed.netloc != request.url.netloc and not (os.environ.get("ECHOTRACE_DEV") == "1" and parsed.port == 5173):
                return JSONResponse({"ok": False, "error": {"code": "INVALID_ORIGIN", "message": "请求来源与本机服务不一致。"}}, status_code=403)
        if request.headers.get("sec-fetch-site") == "cross-site":
            return JSONResponse({"ok": False, "error": {"code": "INVALID_ORIGIN", "message": "拒绝跨站请求。"}}, status_code=403)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; connect-src 'self'; font-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'"
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(AppError)
    async def application_error(request, exc):
        return JSONResponse({"ok": False, "error": {"code": exc.code, "message": exc.message}}, status_code=exc.status)

    @app.exception_handler(RequestValidationError)
    @app.exception_handler(ValidationError)
    async def validation_error(request, exc):
        details = [{"field": ".".join(map(str, e["loc"])), "message": e["msg"]} for e in exc.errors()]
        return JSONResponse({"ok": False, "error": {"code": "INVALID_INPUT", "message": "输入参数不符合要求。", "details": details}}, status_code=422)

    @app.exception_handler(HTTPException)
    async def http_error(request, exc):
        return JSONResponse({"ok": False, "error": {"code": "HTTP_ERROR", "message": "请求地址不存在或方法不支持。"}}, status_code=exc.status_code)

    @app.exception_handler(Exception)
    async def unexpected_error(request, exc):
        return JSONResponse({"ok": False, "error": {"code": "INTERNAL_ERROR", "message": f"本地服务处理失败（{type(exc).__name__}）。数据仍保留，请重试或查看任务状态。"}}, status_code=500)

    app.include_router(router)
    candidates = [Path(__file__).parent / "static", Path(__file__).parents[1] / "web" / "dist"]
    static = next((p for p in candidates if (p / "index.html").exists()), None)
    if static:
        app.mount("/assets", StaticFiles(directory=static / "assets"), name="assets")

    @app.get("/{path:path}")
    async def frontend(path: str):
        if path.startswith("api/"):
            raise AppError("NOT_FOUND", "接口不存在。", 404)
        if static:
            file = (static / path).resolve()
            if file.is_relative_to(static.resolve()) and file.is_file():
                return FileResponse(file)
            return FileResponse(static / "index.html")
        return JSONResponse({"ok": False, "error": {"code": "FRONTEND_NOT_BUILT", "message": "请运行 python scripts/build.py 构建前端，然后重新启动。"}}, status_code=503)
    return app
