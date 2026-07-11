from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
import sqlite3

from data_generation_agent.harness.artifacts import ArtifactStore
from data_generation_agent.harness.db import initialize_database
from data_generation_agent.providers import ModelGatewayError, ModelRequest, ModelResponse
from data_generation_agent.tools.qwen_passrate_review import (
    JUDGE_CONCURRENCY,
    QWEN_MAX_TOKENS,
    PassratePolicy,
    PassrateRunner,
    load_scoring_prompt,
    render_scoring_prompt,
    trial_id,
    validate_input,
)


ROOT = Path(__file__).resolve().parents[1]


class FakeGateway:
    def __init__(self, outputs: list[str | Exception]) -> None:
        self.outputs = list(outputs); self.requests: list[ModelRequest] = []
    def complete(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request); value=self.outputs.pop(0)
        if isinstance(value,Exception): raise value
        return ModelResponse(value,f"response-{len(self.requests)}",request.model)


def _input() -> dict[str, object]:
    return json.loads((ROOT/"tests/fixtures/qwen_passrate_smoke.json").read_text(encoding="utf-8"))


def _eval(score: int) -> str:
    grade="优秀" if score>=90 else "良好" if score>=75 else "一般" if score>=60 else "较差" if score>=40 else "错误"
    return json.dumps({"score":score,"grade":grade,"reason":"Compared against the GT answer.","major_missing_points":[],"major_errors":[],"can_accept":score>=75},ensure_ascii=False)


@pytest.fixture
def runtime(tmp_path: Path):
    connection=initialize_database(tmp_path/"state.sqlite3"); artifacts=ArtifactStore(tmp_path/"artifacts",connection)
    try: yield connection,artifacts
    finally: connection.close()


def test_trial_identity_binds_complete_composite_key() -> None:
    value=validate_input(_input()); policy=PassratePolicy()
    base=trial_id(value,policy,0)
    assert len(base)==64 and base!=trial_id(value,policy,1)
    changed=dict(value,revision_id="other")
    assert base!=trial_id(changed,policy,0)
    changed=dict(value,reference_answer_hash="f"*64)
    assert base!=trial_id(changed,policy,0)
    assert base!=trial_id(value,PassratePolicy(qwen_model="other"),0)
    assert base!=trial_id(value,PassratePolicy(judge_model="other"),0)


def test_five_trials_aggregate_reproducible_passrate_and_distributions(runtime) -> None:
    connection,artifacts=runtime; qwen=FakeGateway([f"answer-{i}" for i in range(5)]); judge=FakeGateway([_eval(s) for s in (95,80,50,20,90)])
    runner=PassrateRunner(connection,artifacts,qwen,judge,load_scoring_prompt(ROOT/"configs/prompts/deepseek_qwen_gt_score_v1.txt"))
    result=runner.run(_input(),PassratePolicy())
    assert result["decision"]=="PASS" and result["passrate"]==pytest.approx(.6)
    assert (result["requested_trials"],result["completed_trials"],result["valid_trials"],result["passed_trials"])==(5,5,5,3)
    assert result["score_summary"]["score_buckets"]=={"0-39":1,"40-59":1,"60-74":0,"75-89":1,"90-100":2}
    assert all(request.max_tokens==QWEN_MAX_TOKENS for request in qwen.requests)
    assert all(request.max_tokens==2048 for request in judge.requests)
    assert result["judge_concurrency"]==JUDGE_CONCURRENCY==20
    repeated=runner.run(_input(),PassratePolicy())
    assert repeated["result_id"]==result["result_id"] and len(qwen.requests)==5 and len(judge.requests)==5
    with pytest.raises(sqlite3.IntegrityError,match="immutable"):
        connection.execute("UPDATE passrate_results SET decision='REJECT' WHERE result_id=?",(result["result_id"],))
    with pytest.raises(sqlite3.IntegrityError,match="cannot be deleted"):
        connection.execute("DELETE FROM passrate_trials WHERE trial_id=?",(result["trial_ids"][0],))


def test_crash_after_confirmed_answer_resumes_without_duplicate_qwen(runtime) -> None:
    connection,artifacts=runtime; qwen=FakeGateway([f"answer-{i}" for i in range(5)]); judge=FakeGateway([_eval(80) for _ in range(5)])
    runner=PassrateRunner(connection,artifacts,qwen,judge,load_scoring_prompt(ROOT/"configs/prompts/deepseek_qwen_gt_score_v1.txt"))
    with pytest.raises(RuntimeError,match="crash"):
        runner.run(_input(),PassratePolicy(),after_answer=lambda _: (_ for _ in ()).throw(RuntimeError("crash")))
    assert len(qwen.requests)==1
    result=runner.run(_input(),PassratePolicy())
    assert result["completed_trials"]==5 and len(qwen.requests)==5 and len(judge.requests)==5


def test_judge_error_retries_only_judge_and_invalid_gt_never_calls(runtime) -> None:
    connection,artifacts=runtime; qwen=FakeGateway([f"answer-{i}" for i in range(3)]); judge=FakeGateway([ModelGatewayError("temporary"),_eval(80),_eval(80),_eval(80)])
    policy=PassratePolicy(requested_trials=3,minimum_valid_trials=3,target_passrate_min=0,target_passrate_max=1)
    runner=PassrateRunner(connection,artifacts,qwen,judge,load_scoring_prompt(ROOT/"configs/prompts/deepseek_qwen_gt_score_v1.txt"))
    first=runner.run(_input(),policy); assert first["decision"]=="ERROR" and first["completed_trials"]==2
    second=runner.run(_input(),policy); assert second["completed_trials"]==3 and len(qwen.requests)==3 and len(judge.requests)==4
    broken=_input(); broken["synthesis_result"]["reference_answer"]=""
    q2=FakeGateway([]); j2=FakeGateway([])
    with pytest.raises(Exception,match="empty"):
        PassrateRunner(connection,artifacts,q2,j2,runner.scoring_prompt).run(broken,policy)
    assert q2.requests==j2.requests==[]


def test_invalid_judge_contract_fails_closed_and_band_can_reject(runtime) -> None:
    connection,artifacts=runtime
    bad=json.dumps({"score":90,"grade":"错误","reason":"bad","major_missing_points":[],"major_errors":[],"can_accept":True},ensure_ascii=False)
    policy=PassratePolicy(requested_trials=1,minimum_valid_trials=1,target_passrate_min=0,target_passrate_max=1)
    runner=PassrateRunner(connection,artifacts,FakeGateway(["answer"]),FakeGateway([bad]),load_scoring_prompt(ROOT/"configs/prompts/deepseek_qwen_gt_score_v1.txt"))
    result=runner.run(_input(),policy); assert result["decision"]=="ERROR" and result["valid_trials"]==0
    # A new policy changes the composite key and can deterministically reject an easy item.
    runner2=PassrateRunner(connection,artifacts,FakeGateway(["answer"]),FakeGateway([_eval(95)]),runner.scoring_prompt)
    easy=runner2.run(_input(),PassratePolicy(requested_trials=1,minimum_valid_trials=1,target_passrate_min=0,target_passrate_max=.8))
    assert easy["decision"]=="REJECT" and easy["passrate"]==1


def test_scoring_prompt_is_exact_bundled_skill_prompt() -> None:
    project=load_scoring_prompt(ROOT/"configs/prompts/deepseek_qwen_gt_score_v1.txt")
    reference=(Path.home()/".codex/skills/gemini-gt-qwen-evaluator/references/scoring_prompt.md").read_text(encoding="utf-8")
    expected=reference.split("```text\n",1)[1].rsplit("\n```",1)[0]
    assert project==expected


def test_empty_qwen_answer_is_still_sent_to_judge(runtime) -> None:
    connection,artifacts=runtime
    judge=FakeGateway([_eval(10)])
    runner=PassrateRunner(connection,artifacts,FakeGateway([""]),judge,load_scoring_prompt(ROOT/"configs/prompts/deepseek_qwen_gt_score_v1.txt"))
    result=runner.run(_input(),PassratePolicy(requested_trials=1,minimum_valid_trials=1,target_passrate_min=0,target_passrate_max=1))
    assert result["completed_trials"]==1 and result["passed_trials"]==0
    assert "【模型回答】" in judge.requests[0].user_prompt


def test_prompt_placeholders_inside_untrusted_answers_are_not_reexpanded() -> None:
    rendered=render_scoring_prompt("Q={{question}} GT={{gt_answer}} M={{model_answer}}","{{gt_answer}}","GT","{{question}}")
    assert rendered=="Q={{gt_answer}} GT=GT M={{question}}"


def test_passrate_runner_rejects_cross_database_artifact_store(tmp_path: Path) -> None:
    first=initialize_database(tmp_path/"first.sqlite3"); second=initialize_database(tmp_path/"second.sqlite3")
    try:
        with pytest.raises(Exception,match="share one connection"):
            PassrateRunner(first,ArtifactStore(tmp_path/"artifacts",second),FakeGateway([]),FakeGateway([]),"{{question}}{{gt_answer}}{{model_answer}}")
    finally:
        first.close(); second.close()
