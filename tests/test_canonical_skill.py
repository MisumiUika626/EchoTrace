import hashlib

import pytest

from echotrace.distill.canonical_skill import (
    REQUIRED_CANONICAL_FILES,
    build_distill_manifest,
    validate_canonical_skill,
)
from echotrace.util import write_json


AUTHORIZED = [
    {"id": "m000001", "sequence": 0, "sender": "them", "ts": 1, "text": "先确认时间。"},
    {"id": "m000002", "sequence": 1, "sender": "them", "ts": 2, "text": "变化也没关系。"},
]


def canonical_tree(target):
    for relative in REQUIRED_CANONICAL_FILES:
        path = target / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# 内容\n\nevidence: m000001\n", encoding="utf-8")
    write_json(target / "skill/references/evidence.json", {
        "evidence_messages": AUTHORIZED,
    })
    for relative in (
        "persona.json", "radar.json", "metrics.json", "self_style.json",
        "skill_quality.json",
    ):
        write_json(target / relative, {"evidence": ["m000001"]})
    (target / "quotes.jsonl").write_text(
        '{"text":"先确认时间。","evidence":["m000001"]}\n', encoding="utf-8"
    )
    skill_hash = hashlib.sha256((target / "skill/SKILL.md").read_bytes()).hexdigest()
    quality = {"passed": True, "semantic_passed": True, "nuwa_status": "passed"}
    report = {"status": "passed", "validation": {"skill_sha256": skill_hash}}
    return quality, report


@pytest.mark.parametrize("broken", ["skill/SKILL.md", "skill/references/research/03-expression-dna.md"])
def test_missing_or_empty_required_file_fails_canonical_gate(tmp_path, broken):
    quality, report = canonical_tree(tmp_path)
    path = tmp_path / broken
    if broken.endswith("SKILL.md"):
        path.unlink()
    else:
        path.write_text("", encoding="utf-8")

    result = validate_canonical_skill(tmp_path, AUTHORIZED, quality, report)

    assert result["complete"] is False
    assert result["gates"]["canonical_files"] is False
    assert broken in result["failures"]


def test_canonical_citations_must_resolve_in_scope_and_portable_packet(tmp_path):
    quality, report = canonical_tree(tmp_path)
    skill = tmp_path / "skill/SKILL.md"
    skill.write_text("# 人物\n\nevidence: m000001, m000002\n", encoding="utf-8")
    report["validation"]["skill_sha256"] = hashlib.sha256(skill.read_bytes()).hexdigest()
    write_json(tmp_path / "skill/references/evidence.json", {
        "evidence_messages": AUTHORIZED[:1],
    })

    missing_portable = validate_canonical_skill(tmp_path, AUTHORIZED, quality, report)
    assert missing_portable["complete"] is False
    assert missing_portable["gates"]["evidence_valid"] is False
    assert "m000002" in missing_portable["failures"]

    write_json(tmp_path / "skill/references/evidence.json", {
        "evidence_messages": AUTHORIZED,
    })
    outside_scope = validate_canonical_skill(tmp_path, AUTHORIZED[:1], quality, report)
    assert outside_scope["complete"] is False
    assert outside_scope["gates"]["evidence_valid"] is False
    assert "m000002" in outside_scope["failures"]

    valid = validate_canonical_skill(tmp_path, AUTHORIZED, quality, report)
    assert valid["gates"]["evidence_valid"] is True


@pytest.mark.parametrize("failure", [
    "format", "semantic", "quality_status", "report_status", "skill_hash",
])
def test_nuwa_pass_and_current_skill_hash_are_both_required(tmp_path, failure):
    quality, report = canonical_tree(tmp_path)
    if failure == "format":
        quality["passed"] = False
    elif failure == "semantic":
        quality["semantic_passed"] = False
    elif failure == "quality_status":
        quality["nuwa_status"] = "degraded"
    elif failure == "report_status":
        report["status"] = "stale"
    else:
        report["validation"]["skill_sha256"] = "0" * 64

    result = validate_canonical_skill(tmp_path, AUTHORIZED, quality, report)

    assert result["complete"] is False
    assert result["gates"]["nuwa_quality"] is False


def test_derived_artifacts_must_exist_and_keep_evidence_in_scope(tmp_path):
    quality, report = canonical_tree(tmp_path)
    (tmp_path / "metrics.json").unlink()
    missing = validate_canonical_skill(tmp_path, AUTHORIZED, quality, report)
    assert missing["complete"] is False
    assert missing["gates"]["derived_consistent"] is False
    assert "metrics.json" in missing["failures"]

    write_json(tmp_path / "metrics.json", {"evidence": ["m999999"]})
    outside = validate_canonical_skill(tmp_path, AUTHORIZED, quality, report)
    assert outside["complete"] is False
    assert outside["gates"]["derived_consistent"] is False
    assert "m999999" in outside["failures"]


def test_formal_manifest_records_v13_required_facts_without_guessing():
    internal = {
        "sources": [{"file": "fixture.txt", "sha256": "source-sha", "provenance": "upload"}],
        "source_fingerprint": "messages-sha",
        "scope": {"start": 10, "end": 20},
        "sampled": 600,
        "total": 600,
        "chunks": ["00000.json", "00001.json"],
        "map_total": 2,
        "map_succeeded": 2,
        "failed_chunks": [],
        "fragmented": {"m000003": 3},
        "plan_mode": "Chunked",
        "evidence_validated": True,
        "phase4_complete": True,
        "chunk_descriptors": [{
            "stage_budgets": {"full_map": {
                "context_capacity": 131072,
                "capacity_source": "explicit",
                "capacity_verified": True,
                "estimator": "characters-v1",
                "approximate": True,
                "output_reserve": 4096,
                "safety_margin": 1311,
            }},
        }],
    }
    parameters = {
        "model": "fiction-model",
        "prompts": {"full_map": "prompt-sha"},
        "nuwa_template_version": "echotrace-nuwa-local-v1",
    }
    metadata = {
        "contact_id": 7,
        "session_id": "person-7",
        "distill_mode": "full",
        "tokens": 3210,
    }
    canonical = {
        "complete": True,
        "gates": {"canonical_files": True, "evidence_valid": True},
        "failures": [],
    }
    result = build_distill_manifest(
        internal=internal,
        parameters=parameters,
        metadata=metadata,
        nuwa_report={"method": "echotrace-nuwa-local-v1", "status": "passed"},
        quality={"passed": True, "semantic_passed": True},
        canonical=canonical,
        skill_path="skill/SKILL.md",
        built_at="2026-10-25T12:00:00+08:00",
    )

    assert result["schema_version"] == "echotrace-distill-v13"
    assert result["person"] == {"contact_id": 7, "session_id": "person-7"}
    assert result["built_at"] == "2026-10-25T12:00:00+08:00"
    assert result["input"] == {
        "sources": internal["sources"],
        "source_fingerprints": ["source-sha"],
        "normalized_messages_sha256": "messages-sha",
        "scope": {"start": 10, "end": 20},
    }
    assert result["mode"] == "full"
    assert result["coverage"] == {
        "total_messages": 600,
        "first_covered_messages": 600,
        "plan_mode": "Chunked",
        "chunks": 2,
        "chunks_succeeded": 2,
        "failed_chunks": [],
        "fragmented_messages": 1,
        "long_message_fragments": 3,
    }
    assert result["model"] == {
        "id": "fiction-model",
        "prompt_sha256": {"full_map": "prompt-sha"},
        "nuwa_method": "echotrace-nuwa-local-v1",
        "nuwa_template_version": "echotrace-nuwa-local-v1",
    }
    assert result["budget"] == {
        "tokenizer_or_estimator": "characters-v1",
        "context_capacity": 131072,
        "capacity_source": "explicit",
        "capacity_verified": True,
        "output_reserve": 4096,
        "safety_margin": 1311,
        "approximate": True,
    }
    assert result["usage"] == {"tokens": 3210}
    assert result["evidence"] == {"validated": True, "phase4_complete": True}
    assert result["nuwa"] == {"status": "passed", "format_passed": True, "semantic_passed": True}
    assert result["skill_path"] == "skill/SKILL.md"
    assert result["gates"] == canonical["gates"]
    assert result["failures"] == []
    assert result["complete"] is True
    assert result["optional_unknown"] is None


def test_formal_manifest_keeps_missing_legacy_facts_unknown_and_sampled_incomplete():
    result = build_distill_manifest(
        internal={"chunks": ["00000.json"], "sampled": 100, "total": 600},
        parameters={},
        metadata={"distill_mode": "sampled"},
        nuwa_report={},
        quality={},
        canonical={"complete": True, "gates": {"canonical_files": True}, "failures": []},
        skill_path="skill/SKILL.md",
        built_at="2026-10-25T13:00:00+08:00",
    )

    assert result["complete"] is False
    assert result["input"]["normalized_messages_sha256"] is None
    assert result["budget"]["context_capacity"] is None
    assert result["budget"]["approximate"] is None
    assert result["usage"]["tokens"] is None
    assert result["coverage"]["chunks_succeeded"] is None
    assert result["legacy_note"] == "缺失字段保持 unknown；sampled 仅为预览。"
