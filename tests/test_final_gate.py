from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import pytest

from data_generation_agent.tools.final_gate import FinalGatePolicy, evaluate


def _input() -> dict[str, object]:
    policy=FinalGatePolicy(); revision="revision-1"; answer_hash="a"*64
    outputs={
        "quality_review":{"status":"COMPLETED","decision":"ACCEPT"},
        "difficulty_prescreen":{"status":"COMPLETED","decision":"HARD","gate_decision":"PASS","passrate":None},
        "consistency_review":{"status":"COMPLETED","decision":"PASS"},
        "answer_synthesis":{"status":"COMPLETED","decision":"SYNTHESIZED","reference_answer_hash":answer_hash},
        "qwen_passrate_review":{"status":"COMPLETED","decision":"PASS","reference_answer_hash":answer_hash,"requested_trials":5,"minimum_valid_trials":3,"completed_trials":5,"valid_trials":5,"passed_trials":3,"passrate":.6,"target_band":[.2,.8]},
    }
    stages={stage:{"revision_id":revision,"policy_digest":policy.digest,"tool":policy.stage_versions[stage],"result_id":f"{stage}-result","output":output} for stage,output in outputs.items()}
    return {"candidate_id":"candidate-1","revision_id":revision,"policy_digest":policy.digest,"stage_results":stages}


def test_only_complete_same_revision_policy_evidence_is_final_accepted() -> None:
    first=evaluate(_input()); second=evaluate(_input())
    assert first==second and first["terminal_decision"]=="FINAL_ACCEPTED"
    assert first["accepted"] is True and first["issue_codes"]==[] and first["network_used"] is False


@pytest.mark.parametrize(
    ("mutation","terminal"),
    [
        (lambda v: v["stage_results"].pop("quality_review"),"BLOCKED_MISSING_STAGE"),
        (lambda v: v["stage_results"]["quality_review"].update(revision_id="old"),"REJECTED_STALE_RESULT"),
        (lambda v: v.update(policy_digest="f"*64),"REJECTED_POLICY_MISMATCH"),
        (lambda v: v["stage_results"]["quality_review"].update(tool="wrong@1.0.0"),"REJECTED_POLICY_MISMATCH"),
        (lambda v: v["stage_results"]["consistency_review"]["output"].update(status="ERROR",decision="ERROR"),"QUARANTINED_STAGE_ERROR"),
        (lambda v: v["stage_results"]["quality_review"]["output"].update(decision="REJECT"),"REJECTED_STAGE"),
        (lambda v: v["stage_results"]["answer_synthesis"]["output"].update(reference_answer_hash="b"*64),"QUARANTINED_INVALID_EVIDENCE"),
        (lambda v: v["stage_results"]["qwen_passrate_review"]["output"].update(completed_trials=4),"QUARANTINED_INVALID_EVIDENCE"),
        (lambda v: v["stage_results"]["qwen_passrate_review"]["output"].update(passrate=.7),"QUARANTINED_INVALID_EVIDENCE"),
    ],
)
def test_truth_table_never_accepts_invalid_or_incomplete_evidence(mutation,terminal: str) -> None:
    value=deepcopy(_input()); mutation(value); result=evaluate(value)
    assert result["terminal_decision"]==terminal and result["accepted"] is False


def test_placeholder_or_missing_stage_acceptance_count_is_zero() -> None:
    cases=[]
    for stage in FinalGatePolicy().stage_versions:
        value=deepcopy(_input()); value["stage_results"].pop(stage); cases.append(evaluate(value))
        value=deepcopy(_input()); value["stage_results"][stage]["output"]={"status":"BLOCKED","decision":"ERROR"}; cases.append(evaluate(value))
    assert sum(result["accepted"] for result in cases)==0


def test_final_gate_uses_no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    import socket
    import urllib.request
    def forbidden(*args,**kwargs): raise AssertionError("network used")
    monkeypatch.setattr(socket,"socket",forbidden)
    monkeypatch.setattr(urllib.request,"urlopen",forbidden)
    assert evaluate(_input())["accepted"] is True


def test_no_other_model_tool_emits_final_accepted() -> None:
    tools=Path(__file__).resolve().parents[1]/"src/data_generation_agent/tools"
    offenders=[]
    for path in tools.glob("*.py"):
        if path.name!="final_gate.py" and "FINAL_ACCEPTED" in path.read_text(encoding="utf-8"):
            offenders.append(path.name)
    assert offenders==[]
