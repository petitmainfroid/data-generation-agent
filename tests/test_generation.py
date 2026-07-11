from __future__ import annotations

import json
from pathlib import Path

import pytest

from data_generation_agent.generation import GenerationError, GenerationPolicy, GenerationRequest, GenerationRunner
from data_generation_agent.harness.artifacts import ArtifactStore
from data_generation_agent.harness.budgets import BudgetExceededError, BudgetManager
from data_generation_agent.harness.db import initialize_database
from data_generation_agent.harness.service import HarnessService
from data_generation_agent.knowledge.models import KnowledgeDocument, PersonaTemplate
from data_generation_agent.knowledge.store import KnowledgeStore
from data_generation_agent.providers import ModelGatewayError, ModelRequest, ModelResponse


class FakeGateway:
    def __init__(self, outputs: list[str | Exception]) -> None:
        self.outputs=list(outputs); self.requests: list[ModelRequest]=[]
    def complete(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request); value=self.outputs.pop(0)
        if isinstance(value,Exception): raise value
        return ModelResponse(value,f"generation-response-{len(self.requests)}",request.model)


def _output(question: str="A planner observes lead-time demand of 100 units and safety stock of 20 units. What is the reorder point?") -> str:
    return json.dumps({"question":question,"question_type":"numeric_calculation","citations":["handbook@rev-1#chunk-1"],"constraint_coverage":["grounded","self-contained","numeric"],"generation_rationale":"Tests application of the cited reorder-point rule."})


def _persona(version: str="1.0.0") -> PersonaTemplate:
    return PersonaTemplate.from_mapping({"schema_version":"persona_template.v1","persona_id":"planner","version":version,"role":"Supply-chain planner","objectives":["Write verifiable inventory questions"],"constraints":["State all numeric inputs"],"allowed_question_types":["numeric_calculation"],"tone":"Precise","forbidden_behaviors":["Claim review passed"]})


def _request(persona_id: str, *, job_id: str="job-1", seed_id: str="seed-1", seed_question: str="How should reorder points be calculated?") -> GenerationRequest:
    return GenerationRequest(job_id,seed_id,seed_question,"numeric_calculation",persona_id,("SNAPSHOT",),"reorder point safety stock")


@pytest.fixture
def runtime(tmp_path: Path):
    connection=initialize_database(tmp_path/"state.sqlite3"); artifacts=ArtifactStore(tmp_path/"artifacts",connection); knowledge=KnowledgeStore(connection,artifacts)
    snapshot,_=knowledge.commit_document(KnowledgeDocument("handbook","rev-1","Inventory planning","Reorder point equals lead-time demand plus safety stock."))
    persona,_=knowledge.commit_persona(_persona())
    service=HarnessService(connection,tmp_path/"artifacts"); service.create_job("job-1",job_type="GENERATE"); service.start_job("job-1")
    BudgetManager(connection).create("job-1","generation_tokens","token",32768)
    try: yield connection,artifacts,knowledge,snapshot,persona,tmp_path
    finally: connection.close()


def _with_snapshot(request: GenerationRequest,snapshot: str) -> GenerationRequest:
    return GenerationRequest(request.job_id,request.seed_id,request.seed_question,request.question_type,request.persona_snapshot_id,(snapshot,),request.retrieval_query)


def test_grounded_generation_persists_provenance_budget_and_review_entry(runtime) -> None:
    connection,artifacts,knowledge,snapshot,persona,_=runtime; gateway=FakeGateway([_output()]); runner=GenerationRunner(connection,artifacts,knowledge,gateway)
    result=runner.generate(_with_snapshot(_request(persona),snapshot))
    assert result["accepted"] is False and result["next_stage"]=="QUALITY_REVIEW"
    assert result["seed_id"]=="seed-1" and result["persona_snapshot_id"]==persona and result["knowledge_snapshot_ids"]==[snapshot]
    candidate=connection.execute("SELECT status FROM candidates WHERE candidate_id=?",(result["candidate_id"],)).fetchone(); assert candidate["status"]=="QUALITY_REVIEW"
    assert connection.execute("SELECT status FROM revisions WHERE revision_id=?",(result["revision_id"],)).fetchone()["status"]=="ACTIVE"
    budget=BudgetManager(connection).get("job-1","generation_tokens"); assert budget.consumed_amount==8192 and budget.reserved_amount==0
    assert 'task_context untrusted="true"' in gateway.requests[0].user_prompt
    assert gateway.requests[0].max_tokens==8192
    repeated=runner.generate(_with_snapshot(_request(persona),snapshot)); assert repeated==result and len(gateway.requests)==1


def test_crash_after_model_confirmation_resumes_without_second_call(runtime) -> None:
    connection,artifacts,knowledge,snapshot,persona,_=runtime; gateway=FakeGateway([_output()]); runner=GenerationRunner(connection,artifacts,knowledge,gateway); request=_with_snapshot(_request(persona),snapshot)
    with pytest.raises(RuntimeError,match="crash"):
        runner.generate(request,after_model_confirmed=lambda _: (_ for _ in ()).throw(RuntimeError("crash")))
    assert len(gateway.requests)==1 and BudgetManager(connection).get("job-1","generation_tokens").reserved_amount==8192
    result=runner.generate(request); assert result["next_stage"]=="QUALITY_REVIEW" and len(gateway.requests)==1
    assert BudgetManager(connection).get("job-1","generation_tokens").reserved_amount==0


def test_gateway_error_releases_budget_and_retry_succeeds(runtime) -> None:
    connection,artifacts,knowledge,snapshot,persona,_=runtime; gateway=FakeGateway([ModelGatewayError("temporary"),_output()]); runner=GenerationRunner(connection,artifacts,knowledge,gateway); request=_with_snapshot(_request(persona),snapshot)
    with pytest.raises(GenerationError,match="temporary"): runner.generate(request)
    budget=BudgetManager(connection).get("job-1","generation_tokens"); assert budget.reserved_amount==budget.consumed_amount==0
    assert runner.generate(request)["next_stage"]=="QUALITY_REVIEW" and len(gateway.requests)==2


def test_budget_exhaustion_and_secret_preflight_never_call_model(runtime) -> None:
    connection,artifacts,knowledge,snapshot,persona,tmp_path=runtime
    service=HarnessService(connection,tmp_path/"artifacts"); service.create_job("small",job_type="GENERATE"); service.start_job("small"); BudgetManager(connection).create("small","generation_tokens","token",100)
    gateway=FakeGateway([]); runner=GenerationRunner(connection,artifacts,knowledge,gateway)
    with pytest.raises(BudgetExceededError): runner.generate(_with_snapshot(_request(persona,job_id="small"),snapshot))
    assert connection.execute("SELECT status FROM candidates WHERE job_id='small'").fetchone()["status"]=="BUDGET_EXHAUSTED"
    secret="sk-fake-generation-secret"
    with pytest.raises(Exception,match="forbidden secret"): runner.generate(_with_snapshot(_request(persona,seed_id="secret",seed_question=secret),snapshot),forbidden_values=(secret,))
    assert gateway.requests==[]


def test_exact_duplicate_is_rejected_without_entering_review(runtime) -> None:
    connection,artifacts,knowledge,snapshot,persona,tmp_path=runtime; service=HarnessService(connection,tmp_path/"artifacts"); service.create_job("job-2",job_type="GENERATE"); service.start_job("job-2"); BudgetManager(connection).create("job-2","generation_tokens","token",32768)
    gateway=FakeGateway([_output(),_output()]); runner=GenerationRunner(connection,artifacts,knowledge,gateway)
    first=runner.generate(_with_snapshot(_request(persona),snapshot)); second=runner.generate(_with_snapshot(_request(persona,job_id="job-2"),snapshot))
    assert first["next_stage"]=="QUALITY_REVIEW"
    assert second["terminal_decision"]=="REJECTED_DUPLICATE" and second["next_stage"] is None
    assert connection.execute("SELECT status FROM candidates WHERE candidate_id=?",(second["candidate_id"],)).fetchone()["status"]=="REJECTED_DUPLICATE"
    assert connection.execute("SELECT count(*) FROM generated_question_registry").fetchone()[0]==1


def test_invalid_model_output_is_terminal_and_never_creates_revision(runtime) -> None:
    connection,artifacts,knowledge,snapshot,persona,_=runtime; gateway=FakeGateway(["not-json"]); runner=GenerationRunner(connection,artifacts,knowledge,gateway); request=_with_snapshot(_request(persona),snapshot)
    with pytest.raises(GenerationError,match="strict JSON"): runner.generate(request)
    assert connection.execute("SELECT count(*) FROM revisions").fetchone()[0]==0
    assert connection.execute("SELECT status FROM candidates").fetchone()["status"]=="FAILED"
    with pytest.raises(GenerationError,match="terminal"): runner.generate(request)
    assert len(gateway.requests)==1


def test_generation_runner_requires_shared_database(tmp_path: Path) -> None:
    first=initialize_database(tmp_path/"first.sqlite3"); second=initialize_database(tmp_path/"second.sqlite3")
    try:
        artifacts=ArtifactStore(tmp_path/"artifacts",second); knowledge=KnowledgeStore(second,artifacts)
        with pytest.raises(GenerationError,match="share one database"): GenerationRunner(first,artifacts,knowledge,FakeGateway([]))
    finally: first.close(); second.close()


def test_persona_scope_and_model_secret_output_fail_before_publication(runtime) -> None:
    connection,artifacts,knowledge,snapshot,persona,_=runtime
    other,_=knowledge.commit_persona(PersonaTemplate.from_mapping({"schema_version":"persona_template.v1","persona_id":"text-only","version":"1.0.0","role":"Reviewer","objectives":["Write reasoning questions"],"constraints":["Be precise"],"allowed_question_types":["logical_reasoning"],"tone":"Precise","forbidden_behaviors":["Invent facts"]}))
    runner=GenerationRunner(connection,artifacts,knowledge,FakeGateway([]))
    with pytest.raises(GenerationError,match="does not allow"):
        runner.generate(_with_snapshot(_request(other,seed_id="persona-scope"),snapshot))
    secret="sk-forbidden-output-secret"
    gateway=FakeGateway([_output(question=f"Leaked {secret}")]); runner=GenerationRunner(connection,artifacts,knowledge,gateway)
    with pytest.raises(GenerationError,match="forbidden secret"):
        runner.generate(_with_snapshot(_request(persona,seed_id="output-secret"),snapshot),forbidden_values=(secret,))
    assert connection.execute("SELECT count(*) FROM revisions").fetchone()[0]==0
    assert secret not in "".join(path.read_text(encoding="utf-8",errors="ignore") for path in artifacts.root.rglob("*") if path.is_file())
