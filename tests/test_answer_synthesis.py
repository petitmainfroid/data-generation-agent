from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from data_generation_agent.providers import ModelGatewayError, ModelRequest, ModelResponse
from data_generation_agent.tools.answer_synthesis import (
    ANSWER_MAX_TOKENS,
    SynthesisConfig,
    run_batch,
    synthesize_one,
)


class FakeGateway:
    def __init__(self, outputs: list[str | Exception]) -> None:
        self.outputs = list(outputs)
        self.requests: list[ModelRequest] = []

    def complete(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        value = self.outputs.pop(0)
        if isinstance(value, Exception):
            raise value
        return ModelResponse(value, "answer-response-1", request.model)


def _evidence(text: str = "Reorder point equals lead-time demand plus safety stock.") -> dict[str, str]:
    return {"citation": "handbook@rev-1#chunk-1", "text": text, "text_hash": hashlib.sha256(text.encode()).hexdigest()}


def _row(**changes: object) -> dict[str, object]:
    revision = str(changes.get("revision_id", "revision-1"))
    value: dict[str, object] = {
        "candidate_id": "candidate-1", "revision_id": revision,
        "question": "How is reorder point calculated?", "question_type": "industry_knowledge",
        "evidence": [_evidence()],
        "consistency_result": {"revision_id": revision, "result_id": "consistency-1", "status": "COMPLETED", "decision": "PASS"},
    }
    value.update(changes); return value


def _answer(answer_type: str = "text", citation: str = "handbook@rev-1#chunk-1") -> str:
    return json.dumps({"answer_type": answer_type, "reference_answer": "Lead-time demand plus safety stock.", "reasoning": "The cited rule states both terms.", "citations": [citation], "assumptions": []})


def test_synthesis_is_typed_citable_and_always_uses_32768_tokens() -> None:
    gateway = FakeGateway([_answer()])
    result = synthesize_one(_row(), gateway, SynthesisConfig())
    assert result["status"] == "COMPLETED" and result["decision"] == "SYNTHESIZED"
    assert result["answer_type"] == "text" and len(result["reference_answer_hash"]) == 64
    assert gateway.requests[0].max_tokens == ANSWER_MAX_TOKENS == 32768
    assert result["result_id"] == gateway.requests[0].idempotency_key


@pytest.mark.parametrize(("question_type", "answer_type"), [("multiple_choice", "choice"), ("numeric_calculation", "numeric"), ("information_extraction", "structured")])
def test_question_form_requires_matching_answer_type(question_type: str, answer_type: str) -> None:
    result = synthesize_one(_row(question_type=question_type), FakeGateway([_answer(answer_type)]), SynthesisConfig())
    assert result["decision"] == "SYNTHESIZED" and result["answer_type"] == answer_type


@pytest.mark.parametrize("output", ["not-json", _answer("numeric"), _answer(citation="unknown#chunk"), json.dumps({"answer_type": "text", "reference_answer": "", "reasoning": "r", "citations": [], "assumptions": []})])
def test_invalid_empty_or_uncited_answer_fails_closed(output: str) -> None:
    result = synthesize_one(_row(), FakeGateway([output]), SynthesisConfig())
    assert result["status"] == "INVALID" and result["decision"] == "ERROR"
    assert result["reference_answer"] is None


def test_nonpassing_or_stale_consistency_never_calls_model() -> None:
    gateway = FakeGateway([_answer()])
    upstream = dict(_row()["consistency_result"], decision="FAIL")
    result = synthesize_one(_row(consistency_result=upstream), gateway, SynthesisConfig())
    assert result["status"] == "BLOCKED" and result["model_called"] is False
    assert gateway.requests == []


def test_transport_error_retries_then_completed_result_resumes(tmp_path: Path) -> None:
    path = tmp_path / "results.jsonl"
    first_gateway = FakeGateway([ModelGatewayError("temporary")])
    assert run_batch([_row()], gateway=first_gateway, config=SynthesisConfig(), results_path=path)[0]["decision"] == "ERROR"
    second_gateway = FakeGateway([_answer()])
    accepted = run_batch([_row()], gateway=second_gateway, config=SynthesisConfig(), results_path=path)
    assert accepted[0]["decision"] == "SYNTHESIZED"
    no_call = FakeGateway([])
    assert run_batch([_row()], gateway=no_call, config=SynthesisConfig(), results_path=path) == accepted
    assert no_call.requests == []


def test_crash_resume_and_changed_content_protection(tmp_path: Path) -> None:
    path = tmp_path / "results.jsonl"
    row2 = _row(candidate_id="candidate-2", revision_id="revision-2", consistency_result={"revision_id": "revision-2", "result_id": "c2", "status": "COMPLETED", "decision": "PASS"})
    gateway = FakeGateway([_answer(), _answer()])
    with pytest.raises(RuntimeError):
        run_batch([_row(), row2], gateway=gateway, config=SynthesisConfig(), results_path=path, after_persist=lambda _: (_ for _ in ()).throw(RuntimeError("crash")))
    assert len(gateway.requests) == 1
    resumed = run_batch([_row(), row2], gateway=gateway, config=SynthesisConfig(), results_path=path)
    assert [item["decision"] for item in resumed] == ["SYNTHESIZED", "SYNTHESIZED"] and len(gateway.requests) == 2
    changed = run_batch([_row(question="changed")], gateway=FakeGateway([]), config=SynthesisConfig(), results_path=path)
    assert changed[0]["issue_codes"] == ["STALE_RESULT_CONFLICT"]
