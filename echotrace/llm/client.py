import asyncio
import hashlib
import json
import re
from pathlib import Path

import httpx
from pydantic import ValidationError

from echotrace.llm.budget import allowance
from echotrace.llm.nuwa_skill import nuwa_instructions
from echotrace.llm.schemas import SCHEMAS
from echotrace.util import AppError, dumps, now, read_json, write_json


def estimate_tokens(value):
    from echotrace.llm.tokenizer import count_tokens
    return count_tokens(value).tokens


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


def build_request(llm, stage, settings, context):
    """Build the exact initial provider request used by planning and live calls."""
    instruction = nuwa_instructions(stage)
    messages = ([{"role": "system", "content": instruction}] if instruction else [])
    messages.append({"role": "user", "content": llm.render(stage, context)})
    return {
        "model": settings["model"],
        "temperature": settings["temperature"],
        "max_tokens": settings["max_tokens"],
        "response_format": {"type": "json_object"},
        "messages": messages,
        **adapter_parameters(settings),
    }


class LLMClient:
    def __init__(self, config, jobs, transport=None):
        self.config, self.db, self.jobs, self.transport = config, config.db, jobs, transport
        self.budget_lock = asyncio.Lock()
        self.reserved = {}

    def template(self, name):
        if name not in SCHEMAS:
            raise AppError("INVALID_PROMPT", "未知提示词模板。")
        candidates = [self.config.root / "prompts" / f"{name}.md", Path.cwd() / "prompts" / f"{name}.md",
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
    def validate_evidence(value, allowed_ids, path="output"):
        if isinstance(value, dict):
            for key, item in value.items():
                if key == "evidence":
                    if not set(item).issubset(allowed_ids):
                        raise ValueError(f"{path}.evidence 引用了未提供或不属于当前人物的消息 ID")
                else:
                    LLMClient.validate_evidence(item, allowed_ids, f"{path}.{key}")
        elif isinstance(value, list):
            for index, item in enumerate(value):
                LLMClient.validate_evidence(item, allowed_ids, f"{path}[{index}]")

    def repair_items(self, stage, document, allowed, validator, normalizer=None):
        """Identify invalid extraction rows without asking for valid rows again."""
        field = {"distill_chunk": "observations", "extract_quotes": "quotes"}.get(stage)
        if not field or not isinstance(document, dict) or not isinstance(document.get(field), list):
            return None
        if not 1 <= len(document[field]) <= 32:
            return None
        errors = []
        for index, item in enumerate(document[field]):
            try:
                single = SCHEMAS[stage].model_validate({field: [item]}).model_dump()
                self.validate_evidence(single, allowed)
                if normalizer:
                    normalizer(single)
                if validator:
                    validator(single)
            except (ValueError, KeyError, TypeError) as exc:
                detail = type(exc).__name__ if isinstance(exc, ValidationError) else str(exc)[:220]
                errors.append({"index": index, "item": item, "error": detail})
        return {"field": field, "document": document, "errors": errors} if errors else None

    async def call(self, stage, context, allowed_ids, job_id=None, override=None, validator=None, *, output_growth=False, normalizer=None):
        settings = self.settings(override)
        instruction = nuwa_instructions(stage)
        request = build_request(self, stage, settings, context)
        initial_messages = request["messages"]
        identity = [stage, context, initial_messages, {k: v for k, v in settings.items()
                    if k not in ("api_key", "max_tokens", "timeout", "context_window", "concurrency")}]
        recovery = self.db.root / "review" / ("repair-" + hashlib.sha256(dumps(identity).encode()).hexdigest() + ".json")
        growth_path = recovery.with_name(recovery.name.replace("repair-", "output-", 1))
        repair = read_json(recovery)

        def validate_document(document, withdrawn=None, *, require_correction=False):
            parsed = SCHEMAS[stage].model_validate(document).model_dump()
            self.validate_evidence(parsed, set(allowed_ids))
            corrections = normalizer(parsed) if normalizer else []
            if require_correction and not corrections:
                raise ValueError("旧研究输出没有可确定纠正的来源标签冲突")
            if validator:
                validator(parsed)
            # Validate the normalized fields as well (e.g. note length).
            parsed = SCHEMAS[stage].model_validate(parsed).model_dump()
            if corrections:
                write_json(recovery.with_name(recovery.name.replace("repair-", "attribution-", 1)),
                           {"stage": stage, "corrections": corrections, "needs_review": False})
            if withdrawn:
                write_json(recovery.with_name(recovery.name.replace("repair-", "withdrawn-quotes-", 1)),
                           {"stage": stage, "items": withdrawn,
                            "reason": "模型局部修复返回空语录，撤回错误候选；已通过的条目保留。",
                            "needs_review": True})
            recovery.unlink(missing_ok=True)
            growth_path.unlink(missing_ok=True)
            return parsed

        if job_id:
            self.jobs.check(job_id)
        if repair and normalizer:
            # Old failed rows may be locally recoverable without a new model
            # request. Keep the original identity for existing repair checkpoints.
            try:
                return validate_document(repair["document"])
            except (ValueError, KeyError, TypeError):
                candidate = self.repair_items(stage, repair.get("document"), set(allowed_ids), validator, normalizer)
                if candidate:
                    repair = candidate
                    write_json(recovery, repair)
        recover_sources = stage == "nuwa_research" and normalizer is not None
        recover_skill_citations = stage == "nuwa_respond" and bool(allowed_ids)
        if (recover_sources or recover_skill_citations) and validator and job_id and not repair:
            # Nuwa stages retain raw review output, not extraction repair state.
            # Recover only this job/stage/exact context after all validations.
            # Research requires a deterministic label correction; blind answers
            # can recover a previous evidence error against the verified Skill
            # citations. Preserve original review output and never accept length.
            suffix = hashlib.sha256(dumps(context).encode()).hexdigest()[:12]
            review_path = self.db.root / "review" / f"{job_id}-{stage}-{suffix}.json"
            try:
                review = read_json(review_path, {})
                review_error = str(review.get("error", ""))
                evidence_error = ".evidence 引用了未提供或不属于当前人物的消息 ID" in review_error
                if review_error.startswith("模型输出校验失败：") and (recover_sources or evidence_error):
                    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", review["raw"].strip())
                    return validate_document(json.loads(cleaned), require_correction=recover_sources)
            except (ValueError, KeyError, TypeError, AttributeError):
                pass  # Unresolved semantic/structural errors still need repair.
        def repair_messages(state):
            repair_instruction = "只按原 schema 返回这些错误条目的修正版，顺序与 items 一致且数量相同。已通过条目由程序保留，不得再次返回。"
            if stage == "extract_quotes":
                repair_instruction += ('若这些错误条目均无法提供可核实的目标本人原话，可返回 {"quotes":[]} 撤回全部错误条目。'
                                       'evidence 只能使用本次 messages 顶层的 id，不能使用正文或回复引用中的 messageId、referencedMessageId；'
                                       '另一位发言人的回复中嵌入的引文不能替代目标本人的源消息。')
            prompt_context = {**context, "output_repair": {"items": state["errors"],
                "instruction": repair_instruction}}
            return ([{"role": "system", "content": instruction}] if instruction else []) + [{"role": "user", "content": self.render(stage, prompt_context)}]
        if repair:
            request["messages"] = repair_messages(repair)
        # Only unsplittable extraction leaves expand here. Persist their allowance
        # independently of settings so resuming never re-bills smaller failed caps.
        expand_leaf = output_growth and stage in ("distill_chunk", "extract_quotes")
        growth = min(7, max(0, read_json(growth_path, {}).get("growth", 0))) if expand_leaf else 0
        failures = 0
        last_error, raw, last_code = "", "", "LLM_OUTPUT_INVALID"
        async with httpx.AsyncClient(timeout=settings["timeout"], transport=self.transport, trust_env=False, follow_redirects=False) as client:
            for attempt in range(10 if expand_leaf else 3):
                if job_id:
                    self.jobs.check(job_id)
                request["max_tokens"], input_estimate = allowance(stage, settings, estimate_tokens, request, growth)
                reservation = input_estimate + request["max_tokens"]
                if job_id:
                    async with self.budget_lock:
                        job = self.jobs.get(job_id)
                        if job["tokens"] + self.reserved.get(job_id, 0) + reservation > job["payload"].get("token_limit", 200000):
                            raise AppError("TOKEN_BUDGET_EXCEEDED", "已到 token 预算上限，检查点已保存。提高预算后可继续。")
                        self.reserved[job_id] = self.reserved.get(job_id, 0) + reservation
                document, response = None, None
                try:
                    async with asyncio.timeout(settings["timeout"]):
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
                    input_n = usage.get("prompt_tokens", estimate_tokens(request["messages"]))
                    output_n = usage.get("completion_tokens", estimate_tokens(raw))
                    self.db.execute("INSERT INTO token_usage(job_id,model,stage,prompt_tokens,completion_tokens,total_tokens,created_at) VALUES(?,?,?,?,?,?,?)",
                                    (job_id, settings["model"], stage, input_n, output_n, usage.get("total_tokens", input_n + output_n), now()))
                    if body["choices"][0].get("finish_reason") == "length":
                        suffix = hashlib.sha256(dumps(context).encode()).hexdigest()[:12]
                        write_json(self.db.root / "review" / f"{job_id or now()}-{stage}-{suffix}.json",
                                   {"raw": raw, "needs_review": True, "error": "LLM_OUTPUT_TRUNCATED",
                                    "requested_max_tokens": request["max_tokens"], "configured_max_tokens": settings["max_tokens"],
                                    "context_window": settings.get("context_window", 131072), "usage": usage})
                        if expand_leaf:
                            write_json(growth_path, {"growth": growth})
                        guidance = ("当前已达到设置的单次输出上限；可在服务商支持范围内提高上限。"
                                    if request["max_tokens"] >= settings["max_tokens"] else
                                    "本次额度受阶段重试或剩余上下文限制；继续提高设置的输出上限未必有效。")
                        raise AppError("LLM_OUTPUT_TRUNCATED",
                            f"模型服务返回输出截断（finish_reason=length）；本次申请 {request['max_tokens']:,} token，"
                            f"设置上限 {settings['max_tokens']:,} token，上下文窗口 {settings.get('context_window', 131072):,} token。"
                            + guidance + "思考 token 可能与正文共享输出额度，可降低思考等级或缩短自定义提示词。"
                            "增加任务总预算不能解决此次截断；本次用量已记账。", 502)
                    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip())
                    document = json.loads(cleaned)
                    withdrawn = None
                    if repair:
                        field = repair["field"]
                        replacements = document.get(field)
                        retract_quotes = stage == "extract_quotes" and replacements == []
                        if not isinstance(replacements, list) or (not retract_quotes and len(replacements) != len(repair["errors"])):
                            document = None
                            raise ValueError("局部修复必须逐一返回所有错误条目，数量不得变化")
                        merged = {**repair["document"], field: list(repair["document"][field])}
                        if retract_quotes:
                            withdrawn = repair["errors"]
                            rejected = {item["index"] for item in withdrawn}
                            merged[field] = [item for index, item in enumerate(merged[field]) if index not in rejected]
                        else:
                            for error, replacement in zip(repair["errors"], replacements):
                                merged[field][error["index"]] = replacement
                        document = merged
                    return validate_document(document, withdrawn)
                except AppError as exc:
                    if exc.code == "LLM_OUTPUT_TRUNCATED":
                        # Splittable extraction returns to its caller immediately;
                        # leaves grow up to the effective ceiling (at most 8 caps).
                        next_cap, _ = allowance(stage, settings, estimate_tokens, request, growth + 1)
                        if ((stage in ("distill_chunk", "extract_quotes") and not expand_leaf)
                                or (growth >= 7 if expand_leaf else attempt == 2)
                                or next_cap <= request["max_tokens"]):
                            raise
                        growth += 1
                        if expand_leaf:
                            write_json(growth_path, {"growth": growth})
                            continue
                    if exc.code in ("LLM_AUTH_FAILED", "LLM_REQUEST_FAILED") and response is not None and response.status_code < 500:
                        raise
                    last_error = exc.message
                    last_code = exc.code
                except (ValueError, KeyError, TypeError, ValidationError) as exc:
                    if isinstance(exc, ValidationError):
                        first = exc.errors(include_input=False)[0]
                        detail = ".".join(map(str, first["loc"])) + ": " + first["type"]
                    elif isinstance(exc, json.JSONDecodeError):
                        detail = f"JSON 无效（行 {exc.lineno}，列 {exc.colno}）"
                    elif type(exc) is ValueError:
                        detail = str(exc)[:220]
                    else:
                        detail = type(exc).__name__
                    last_error = "模型输出校验失败：" + detail
                    last_code = "LLM_OUTPUT_INVALID"
                    candidate = self.repair_items(stage, document, set(allowed_ids), validator, normalizer)
                    if candidate:
                        repair = candidate
                        write_json(recovery, repair)
                    request["messages"] = repair_messages(repair) if repair else initial_messages + [
                        {"role": "user", "content": "校验失败，请简洁地按 schema 重新输出 JSON，引用只能使用已给出的证据 ID。错误：" + detail}]
                except (httpx.HTTPError, TimeoutError):
                    last_error = "无法连接模型服务，请检查网络和服务地址；数据仍保存在本机。"
                    last_code = "LLM_UNREACHABLE"
                finally:
                    if job_id:
                        async with self.budget_lock:
                            self.reserved[job_id] = max(0, self.reserved.get(job_id, 0) - reservation)
                failures += 1
                if failures >= 3:
                    break
                if attempt < (9 if expand_leaf else 2):
                    await asyncio.sleep(2 ** (failures - 1))
        if raw:
            suffix = hashlib.sha256(dumps(context).encode()).hexdigest()[:12]
            write_json(self.db.root / "review" / f"{job_id or now()}-{stage}-{suffix}.json", {"raw": raw, "needs_review": True, "error": last_error})
        raise AppError(last_code, last_error + " 可修正设置或模板后重试。", 502)

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
