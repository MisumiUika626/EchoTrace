"""Mechanical completion gates for a portable canonical person Skill."""

import hashlib
import json
from pathlib import Path

from echotrace.distill.skill_writer import citation_ids
from echotrace.util import read_json


REQUIRED_CANONICAL_FILES = (
    "skill/SKILL.md",
    "skill/references/quotes.md",
    "skill/references/research/01-persona.md",
    "skill/references/research/02-conversations.md",
    "skill/references/research/03-expression-dna.md",
    "skill/references/research/04-external-views.md",
    "skill/references/research/05-decisions.md",
    "skill/references/research/06-timeline.md",
)

REQUIRED_DERIVED_FILES = (
    "persona.json",
    "radar.json",
    "quotes.jsonl",
    "metrics.json",
    "self_style.json",
    "skill_quality.json",
)


def _explicit_evidence(value):
    if isinstance(value, dict):
        result = set(value.get("evidence", []))
        for key, child in value.items():
            if key != "evidence":
                result.update(_explicit_evidence(child))
        return result
    if isinstance(value, list):
        result = set()
        for child in value:
            result.update(_explicit_evidence(child))
        return result
    return set()


def _derived_evidence(path):
    if path.suffix == ".jsonl":
        result = set()
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                result.update(_explicit_evidence(json.loads(line)))
        return result
    return _explicit_evidence(read_json(path, {}))


def validate_canonical_skill(
    target: Path,
    authorized_messages: list[dict],
    quality: dict,
    nuwa_report: dict,
) -> dict:
    """Return structured local gates without claiming semantic correctness."""
    failures = []
    missing = []
    for relative in REQUIRED_CANONICAL_FILES:
        path = target / relative
        if not path.is_file() or not path.read_text(encoding="utf-8").strip():
            missing.append(relative)
    failures.extend(missing)
    citations = set()
    for relative in REQUIRED_CANONICAL_FILES:
        path = target / relative
        if path.is_file():
            citations.update(citation_ids(path.read_text(encoding="utf-8")))
    portable = read_json(target / "skill/references/evidence.json", {})
    portable_ids = {message.get("id") for message in portable.get("evidence_messages", [])}
    authorized_ids = {message.get("id") for message in authorized_messages}
    invalid_ids = sorted(citations - (portable_ids & authorized_ids), key=str)
    failures.extend(str(evidence_id) for evidence_id in invalid_ids)

    skill_path = target / "skill/SKILL.md"
    current_hash = hashlib.sha256(skill_path.read_bytes()).hexdigest() if skill_path.is_file() else None
    nuwa_valid = all((
        quality.get("passed") is True,
        quality.get("semantic_passed") is True,
        quality.get("nuwa_status") == "passed",
        nuwa_report.get("status") == "passed",
        nuwa_report.get("validation", {}).get("skill_sha256") == current_hash,
    ))
    if not nuwa_valid:
        failures.append("nuwa_quality")

    missing_derived = [relative for relative in REQUIRED_DERIVED_FILES
                       if not (target / relative).is_file()]
    derived_ids = set()
    for relative in REQUIRED_DERIVED_FILES:
        path = target / relative
        if path.is_file():
            derived_ids.update(_derived_evidence(path))
    invalid_derived = sorted(derived_ids - authorized_ids, key=str)
    failures.extend(missing_derived)
    failures.extend(str(evidence_id) for evidence_id in invalid_derived)
    gates = {
        "canonical_files": not missing,
        "evidence_valid": not invalid_ids,
        "nuwa_quality": nuwa_valid,
        "derived_consistent": not missing_derived and not invalid_derived,
    }
    return {"gates": gates, "complete": all(gates.values()), "failures": failures}


def build_distill_manifest(
    *,
    internal: dict,
    parameters: dict,
    metadata: dict,
    nuwa_report: dict,
    quality: dict,
    canonical: dict,
    skill_path: str,
    built_at: str,
) -> dict:
    """Consolidate authoritative task facts without database or provider access."""
    sources = internal.get("sources") or []
    descriptors = internal.get("chunk_descriptors") or []
    first_budget = {}
    if descriptors:
        stages = descriptors[0].get("stage_budgets") or {}
        first_budget = stages.get("full_map") or next(iter(stages.values()), {})
    fragmented = internal.get("fragmented") or {}
    mode = metadata.get("distill_mode")
    complete = mode == "full" and canonical.get("complete") is True
    return {
        "schema_version": "echotrace-distill-v13",
        "person": {
            "contact_id": metadata.get("contact_id"),
            "session_id": metadata.get("session_id"),
        },
        "built_at": built_at,
        "input": {
            "sources": sources,
            "source_fingerprints": [source.get("sha256") for source in sources
                                    if source.get("sha256")],
            "normalized_messages_sha256": internal.get("source_fingerprint"),
            "scope": internal.get("scope") or {},
        },
        "mode": mode,
        "coverage": {
            "total_messages": internal.get("total"),
            "first_covered_messages": internal.get("sampled"),
            "plan_mode": internal.get("plan_mode"),
            "chunks": len(internal.get("chunks") or []),
            "chunks_succeeded": internal.get("map_succeeded"),
            "failed_chunks": internal.get("failed_chunks") or [],
            "fragmented_messages": len(fragmented),
            "long_message_fragments": sum(fragmented.values()),
        },
        "model": {
            "id": parameters.get("model"),
            "prompt_sha256": parameters.get("prompts") or {},
            "nuwa_method": nuwa_report.get("method"),
            "nuwa_template_version": parameters.get("nuwa_template_version"),
        },
        "budget": {
            "tokenizer_or_estimator": first_budget.get("estimator"),
            "context_capacity": first_budget.get("context_capacity"),
            "capacity_source": first_budget.get("capacity_source"),
            "capacity_verified": first_budget.get("capacity_verified"),
            "output_reserve": first_budget.get("output_reserve"),
            "safety_margin": first_budget.get("safety_margin"),
            "approximate": first_budget.get("approximate"),
        },
        "usage": {"tokens": metadata.get("tokens")},
        "evidence": {
            "validated": internal.get("evidence_validated"),
            "phase4_complete": internal.get("phase4_complete"),
        },
        "nuwa": {
            "status": nuwa_report.get("status"),
            "format_passed": quality.get("passed"),
            "semantic_passed": quality.get("semantic_passed"),
        },
        "skill_path": skill_path,
        "gates": canonical.get("gates") or {},
        "failures": canonical.get("failures") or [],
        "complete": complete,
        "optional_unknown": None,
        "legacy_note": ("缺失字段保持 unknown；sampled 仅为预览。"
                        if mode == "sampled" else None),
    }
