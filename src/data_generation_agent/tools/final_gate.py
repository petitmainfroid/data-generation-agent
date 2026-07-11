from __future__ import annotations

import argparse
import hashlib
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from data_generation_agent.knowledge.models import canonical_json

from .common import ToolContractError, atomic_write_json


TOOL_ID = "final_gate"
TOOL_VERSION = "1.0.0"
REQUIRED_STAGES = (
    "quality_review",
    "difficulty_prescreen",
    "consistency_review",
    "answer_synthesis",
    "qwen_passrate_review",
)


@dataclass(frozen=True)
class FinalGatePolicy:
    policy_id: str = "hard_question_final_gate"
    version: str = "1.0.0"
    stage_versions: Mapping[str, str] = field(
        default_factory=lambda: {
            "quality_review": "lao_quality_review@1.1.0",
            "difficulty_prescreen": "lao_difficulty_prescreen@1.1.0",
            "consistency_review": "consistency_review@1.0.0",
            "answer_synthesis": "answer_synthesis@1.0.0",
            "qwen_passrate_review": "qwen_passrate_review@1.0.0",
        }
    )

    def __post_init__(self) -> None:
        if set(self.stage_versions) != set(REQUIRED_STAGES):
            raise ToolContractError("final policy must pin every required stage version")

    @property
    def digest(self) -> str:
        return hashlib.sha256(
            canonical_json(
                {
                    "policy_id": self.policy_id,
                    "version": self.version,
                    "stage_versions": dict(self.stage_versions),
                }
            ).encode()
        ).hexdigest()


def _result(
    candidate_id: str,
    revision_id: str,
    policy: FinalGatePolicy,
    terminal_decision: str,
    issue_codes: list[str],
    stage_result_ids: Mapping[str, str],
) -> dict[str, Any]:
    material = {
        "candidate_id": candidate_id,
        "revision_id": revision_id,
        "policy_digest": policy.digest,
        "terminal_decision": terminal_decision,
        "issue_codes": sorted(set(issue_codes)),
        "stage_result_ids": dict(sorted(stage_result_ids.items())),
    }
    return {
        "schema_version": "final_gate_result.v1",
        "tool_id": TOOL_ID,
        "tool_version": TOOL_VERSION,
        "result_id": hashlib.sha256(canonical_json(material).encode()).hexdigest(),
        **material,
        "accepted": terminal_decision == "FINAL_ACCEPTED",
        "network_used": False,
    }


def evaluate(raw: Mapping[str, Any], policy: FinalGatePolicy | None = None) -> dict[str, Any]:
    policy = policy or FinalGatePolicy()
    candidate_id = raw.get("candidate_id")
    revision_id = raw.get("revision_id")
    if not isinstance(candidate_id, str) or not candidate_id or not isinstance(revision_id, str) or not revision_id:
        raise ToolContractError("final gate candidate_id and revision_id are required")
    stages = raw.get("stage_results")
    if not isinstance(stages, Mapping):
        return _result(candidate_id, revision_id, policy, "BLOCKED_MISSING_STAGE", ["STAGE_RESULTS_MISSING"], {})
    missing = [stage for stage in REQUIRED_STAGES if stage not in stages]
    stage_ids = {
        stage: str(value.get("result_id", ""))
        for stage, value in stages.items()
        if stage in REQUIRED_STAGES and isinstance(value, Mapping)
    }
    if missing:
        return _result(candidate_id, revision_id, policy, "BLOCKED_MISSING_STAGE", [f"MISSING_{stage.upper()}" for stage in missing], stage_ids)
    if set(stages) != set(REQUIRED_STAGES):
        return _result(candidate_id, revision_id, policy, "QUARANTINED_INVALID_EVIDENCE", ["UNKNOWN_STAGE_RESULT"], stage_ids)
    envelopes: dict[str, Mapping[str, Any]] = {}
    for stage in REQUIRED_STAGES:
        envelope = stages[stage]
        if not isinstance(envelope, Mapping) or not isinstance(envelope.get("output"), Mapping):
            return _result(candidate_id, revision_id, policy, "QUARANTINED_INVALID_EVIDENCE", [f"INVALID_{stage.upper()}_ENVELOPE"], stage_ids)
        envelopes[stage] = envelope
    if any(envelope.get("revision_id") != revision_id for envelope in envelopes.values()):
        return _result(candidate_id, revision_id, policy, "REJECTED_STALE_RESULT", ["REVISION_MISMATCH"], stage_ids)
    if raw.get("policy_digest") != policy.digest or any(envelope.get("policy_digest") != policy.digest for envelope in envelopes.values()):
        return _result(candidate_id, revision_id, policy, "REJECTED_POLICY_MISMATCH", ["POLICY_MISMATCH"], stage_ids)
    for stage, envelope in envelopes.items():
        if envelope.get("tool") != policy.stage_versions[stage]:
            return _result(candidate_id, revision_id, policy, "REJECTED_POLICY_MISMATCH", [f"TOOL_VERSION_MISMATCH_{stage.upper()}"], stage_ids)
        if not isinstance(envelope.get("result_id"), str) or not envelope["result_id"]:
            return _result(candidate_id, revision_id, policy, "QUARANTINED_INVALID_EVIDENCE", [f"MISSING_RESULT_ID_{stage.upper()}"], stage_ids)
    outputs = {stage: envelope["output"] for stage, envelope in envelopes.items()}
    for stage, output in outputs.items():
        if output.get("status") in {"ERROR", "INVALID", "BLOCKED"} or output.get("decision") == "ERROR":
            return _result(candidate_id, revision_id, policy, "QUARANTINED_STAGE_ERROR", [f"{stage.upper()}_ERROR"], stage_ids)
    required_passes = {
        "quality_review": outputs["quality_review"].get("status") == "COMPLETED" and outputs["quality_review"].get("decision") == "ACCEPT",
        "difficulty_prescreen": outputs["difficulty_prescreen"].get("status") == "COMPLETED" and outputs["difficulty_prescreen"].get("decision") == "HARD" and outputs["difficulty_prescreen"].get("gate_decision") == "PASS" and outputs["difficulty_prescreen"].get("passrate") is None,
        "consistency_review": outputs["consistency_review"].get("status") == "COMPLETED" and outputs["consistency_review"].get("decision") == "PASS",
        "answer_synthesis": outputs["answer_synthesis"].get("status") == "COMPLETED" and outputs["answer_synthesis"].get("decision") == "SYNTHESIZED",
        "qwen_passrate_review": outputs["qwen_passrate_review"].get("decision") == "PASS",
    }
    failed = [stage for stage, passed in required_passes.items() if not passed]
    if failed:
        return _result(candidate_id, revision_id, policy, "REJECTED_STAGE", [f"{stage.upper()}_NOT_PASSING" for stage in failed], stage_ids)
    synthesis_hash = outputs["answer_synthesis"].get("reference_answer_hash")
    passrate = outputs["qwen_passrate_review"]
    if not isinstance(synthesis_hash, str) or len(synthesis_hash) != 64 or passrate.get("reference_answer_hash") != synthesis_hash:
        return _result(candidate_id, revision_id, policy, "QUARANTINED_INVALID_EVIDENCE", ["REFERENCE_ANSWER_HASH_MISMATCH"], stage_ids)
    requested, completed, valid, passed = (passrate.get(key) for key in ("requested_trials", "completed_trials", "valid_trials", "passed_trials"))
    minimum = passrate.get("minimum_valid_trials")
    if not all(type(value) is int for value in (requested, completed, valid, passed, minimum)) or completed != requested or valid < minimum or passed > valid:
        return _result(candidate_id, revision_id, policy, "QUARANTINED_INVALID_EVIDENCE", ["PASSRATE_COUNTS_INVALID"], stage_ids)
    rate = passrate.get("passrate")
    band = passrate.get("target_band")
    if not isinstance(rate, (int, float)) or isinstance(rate, bool) or not isinstance(band, list) or len(band) != 2 or not band[0] <= rate <= band[1] or abs(rate - passed / valid) > 1e-12:
        return _result(candidate_id, revision_id, policy, "QUARANTINED_INVALID_EVIDENCE", ["PASSRATE_AGGREGATE_INVALID"], stage_ids)
    return _result(candidate_id, revision_id, policy, "FINAL_ACCEPTED", [], stage_ids)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Network-free deterministic final gate.")
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args(argv)


def main() -> None:
    try:
        args = parse_args()
        value = json.loads(args.input.read_text(encoding="utf-8"))
        result = evaluate(value)
        atomic_write_json(args.output, result)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        raise SystemExit(0 if result["accepted"] else 2)
    except (ToolContractError, OSError, json.JSONDecodeError) as exc:
        print(f"final gate error: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc


if __name__ == "__main__":
    main()
