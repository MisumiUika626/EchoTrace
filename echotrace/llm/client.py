import asyncio
import json
import math
import re
from pathlib import Path

import httpx
from pydantic import ValidationError

from echotrace.llm.schemas import SCHEMAS
from echotrace.util import AppError, dumps, now, write_json


def estimate_tokens(value):
    text = value if isinstance(value, str) else dumps(value)
    return math.ceil(sum(1 if ord(c) > 127 else .3 for c in text))


def adapter_parameters(llm):
    if llm["adapter"] == "none":
        return {}
    value = llm["thinking_values"].get(llm["thinking"], llm["thinking"])
    field = llm["thinking_field"]
    if llm["adapter"] == "budget":
        return {"thinking": {"type": "enabled", "budget_tokens": {"low": 1024, "medium": 4096, "high": 8192, "max": 16384}[llm["thinking"]]}}
    if llm["adapter"] == "reasoning_effort":
        field = "reasoning_effort"
    result, current = {}, None
    current = result
    for key in field.split(".")[:-1]:
        current[key] = {}
        current = current[key]
    current[field.split(".")[-1]] = value
    return result


class LLMClient:
    def __init__(self, config, jobs, transport=None):
        self.config, self.db, self.jobs, self.transport = config, config.db, jobs, transport
        self.budget_lock = asyncio.Lock()
        self.reserved = {}

    def template(self, name):
        if name not in SCHEMAS:
            raise AppError("INVALID_PROMPT", "未知提示词模板。")
        candidates = [self.config.prompt_dir() / f"{name}.md", Path.cwd() / "prompts" / f"{name}.md",
                      Path(__file__).parents[1] / "templates" / f"{name}.md"]
        for path in candidates:
            if path.exists():
                return path.read_text(encoding="utf-8")
        raise AppError("PROMPT_NOT_FOUND", f"缺少提示词 {name}.md。")

    def render(self, name, context):
        template = self.template(name)
        values = {"context": dumps(context), "schema": dumps(SCHEMAS[name].model_json_schema())}
        result = re.sub(r"\{\{\s*(\w+)\s*\}\}", lambda m: values.get(m[1], m[0]), template)
        if re.search(r"\{\{\s*\w+\s*\}\}", result):
            raise AppError("INVALID_PROMPT", "提示词含未知变量，只支持 context 与 schema。")
        return result

    def settings(self, override=None):
        settings = self.config.get(True)["llm"]
        settings.update({k: v for k, v in (override or {}).items() if k in ("model", "thinking", "temperature")})
        if not settings["base_url"]:
            raise AppError("LLM_NOT_CONFIGURED", "请先在设置中填写模型服务地址和密钥。")
        if not self.config.get()["privacy"]["acknowledged"]:
            raise AppError("PRIVACY_ACK_REQUIRED", "请先阅读并确认设置页的模型隐私说明。")
        return settings

    @staticmethod
    def validate_evidence(value, allowed_ids):
        if isinstance(value, dict):
            for key, item in value.items():
                if key == "evidence":
                    if not set(item).issubset(allowed_ids):
                        raise ValueError("模型引用了未提供或不属于当前人物的消息 ID")
                else:
                    LLMClient.validate_evidence(item, allowed_ids)
        elif isinstance(value, list):
            for item in value:
                LLMClient.validate_evidence(item, allowed_ids)

    async def call(self, stage, context, allowed_ids, job_id=None, override=None, validator=None):
        settings = self.settings(override)
        prompt = self.render(stage, context)
        request = {"model": settings["model"], "temperature": settings["temperature"], "max_tokens": settings["max_tokens"],
                   "response_format": {"type": "json_object"}, "messages": [{"role": "user", "content": prompt}], **adapter_parameters(settings)}
        if settings["adapter"] == "budget":
            request["max_tokens"] = max(request["max_tokens"], request["thinking"]["budget_tokens"] + 4096)
        last_error, raw = "", ""
        async with httpx.AsyncClient(timeout=settings["timeout"], transport=self.transport, trust_env=False, follow_redirects=False) as client:
            for attempt in range(3):
                if job_id:
                    self.jobs.check(job_id)
                reservation = estimate_tokens(request) + request["max_tokens"]
                if job_id:
                    async with self.budget_lock:
                        job = self.jobs.get(job_id)
                        if job["tokens"] + self.reserved.get(job_id, 0) + reservation > job["payload"].get("token_limit", 200000):
                            raise AppError("TOKEN_BUDGET_EXCEEDED", "已到 token 预算上限，检查点已保存。提高预算后可继续。")
                        self.reserved[job_id] = self.reserved.get(job_id, 0) + reservation
                try:
                    response = await client.post(settings["base_url"].rstrip("/") + "/chat/completions", json=request,
                                                 headers={"Authorization": "Bearer " + settings["api_key"]})
                    if response.status_code in (401, 403):
                        raise AppError("LLM_AUTH_FAILED", "模型密钥无效或没有此模型权限，请检查设置。", 401)
                    if response.status_code == 429:
                        raise AppError("LLM_RATE_LIMITED", "模型服务限流，请稍后继续任务。", 429)
                    if response.status_code >= 400:
                        raise AppError("LLM_REQUEST_FAILED", f"模型服务返回 HTTP {response.status_code}；请核对模型 ID、适配器参数和额度。", 502)
                    body = response.json()
                    raw = body["choices"][0]["message"]["content"] or ""
                    usage = body.get("usage", {})
                    input_n = usage.get("prompt_tokens", estimate_tokens(prompt))
                    output_n = usage.get("completion_tokens", estimate_tokens(raw))
                    self.db.execute("INSERT INTO token_usage(job_id,model,stage,prompt_tokens,completion_tokens,total_tokens,created_at) VALUES(?,?,?,?,?,?,?)",
                                    (job_id, settings["model"], stage, input_n, output_n, usage.get("total_tokens", input_n + output_n), now()))
                    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip())
                    parsed = SCHEMAS[stage].model_validate(json.loads(cleaned)).model_dump()
                    self.validate_evidence(parsed, set(allowed_ids))
                    if validator:
                        validator(parsed)
                    return parsed
                except AppError as exc:
                    if exc.code in ("LLM_AUTH_FAILED", "LLM_REQUEST_FAILED") and (response.status_code < 500):
                        raise
                    last_error = exc.message
                except (ValueError, KeyError, TypeError, ValidationError) as exc:
                    last_error = "模型输出的结构或证据校验失败。"
                    request["messages"] = [{"role": "user", "content": prompt}, {"role": "assistant", "content": raw},
                                           {"role": "user", "content": "校验失败，请按 schema 重新完整输出 JSON，引用只能使用已给出的证据 ID。错误：" + str(exc)[:1400]}]
                except httpx.HTTPError:
                    last_error = "无法连接模型服务，请检查网络和服务地址；数据仍保存在本机。"
                finally:
                    if job_id:
                        async with self.budget_lock:
                            self.reserved[job_id] = max(0, self.reserved.get(job_id, 0) - reservation)
                if attempt < 2:
                    await asyncio.sleep(2 ** attempt)
        if raw:
            write_json(self.db.root / "review" / f"{job_id or now()}-{stage}.json", {"raw": raw, "needs_review": True, "error": last_error})
        raise AppError("LLM_OUTPUT_INVALID" if raw else "LLM_UNREACHABLE", last_error + " 可修正设置或模板后重试。", 502)

    async def test(self):
        settings = self.settings()
        try:
            async with httpx.AsyncClient(timeout=15, trust_env=False, transport=self.transport) as client:
                response = await client.get(settings["base_url"].rstrip("/") + "/models", headers={"Authorization": "Bearer " + settings["api_key"]})
                if response.status_code in (401, 403):
                    raise AppError("LLM_AUTH_FAILED", "密钥无效或权限不足。")
                response.raise_for_status()
                models = [item["id"] for item in response.json().get("data", [])]
                return {"reachable": True, "models": models, "parameters": adapter_parameters(settings),
                        "note": "模型列表接口可用；思考参数是否被模型支持，需以实际调用结果为准。"}
        except httpx.HTTPError as exc:
            raise AppError("LLM_UNREACHABLE", "连通性测试失败，请检查 Base URL、密钥和网络。", 502) from exc
