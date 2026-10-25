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
