from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Sequence

from data_generation_agent.harness.artifacts import ArtifactStore
from data_generation_agent.harness.budgets import BudgetExceededError, BudgetManager
from data_generation_agent.harness.db import transaction
from data_generation_agent.harness.store import HarnessStore
from data_generation_agent.knowledge.models import ALLOWED_QUESTION_TYPES, KnowledgeContractError, canonical_json, digest_text, normalize_text
from data_generation_agent.knowledge.prompt import PromptCompiler, PromptRequest
from data_generation_agent.knowledge.retrieval import DeterministicRetriever, RetrievalQuery
from data_generation_agent.knowledge.store import KnowledgeStore
from data_generation_agent.providers import ModelGateway, ModelGatewayError, ModelRequest


class GenerationError(RuntimeError):
    """A generation request violated its bounded durable contract."""


GENERATION_OUTPUT_SCHEMA = {
    "type": "object",
    "required": ["question", "question_type", "citations", "constraint_coverage", "generation_rationale"],
    "properties": {
        "question": {"type": "string"},
        "question_type": {"type": "string"},
        "citations": {"type": "array", "items": {"type": "string"}},
        "constraint_coverage": {"type": "array", "items": {"type": "string"}},
        "generation_rationale": {"type": "string"},
    },
    "additionalProperties": False,
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()


@dataclass(frozen=True)
class GenerationPolicy:
    version: str = "grounded_generation.v1"
    model: str = "gpt-5.5"
    max_tokens: int = 8192
    timeout_seconds: int = 600
    retrieval_top_k: int = 5

    def __post_init__(self) -> None:
        if not self.version or not self.model:
            raise GenerationError("generation policy version and model are required")
        if not 1 <= self.max_tokens <= 32768 or not 1 <= self.timeout_seconds <= 600:
            raise GenerationError("generation model bounds are invalid")
        if not 1 <= self.retrieval_top_k <= 20:
            raise GenerationError("generation retrieval_top_k is invalid")

    @property
    def digest(self) -> str:
        return _hash(self.__dict__)


@dataclass(frozen=True)
class GenerationRequest:
    job_id: str
    seed_id: str
    seed_question: str
    question_type: str
    persona_snapshot_id: str
    approved_snapshot_ids: tuple[str, ...]
    retrieval_query: str

    def __post_init__(self) -> None:
        for name in ("job_id", "seed_id", "seed_question", "persona_snapshot_id", "retrieval_query"):
            value = getattr(self, name)
            if not isinstance(value, str) or not normalize_text(value):
                raise GenerationError(f"{name} must be non-empty text")
        if self.question_type not in ALLOWED_QUESTION_TYPES:
            raise GenerationError("generation question_type is unknown")
        if not self.approved_snapshot_ids:
            raise GenerationError("generation knowledge scope must not be empty")


class GenerationRunner:
    def __init__(self, connection: sqlite3.Connection, artifacts: ArtifactStore, knowledge: KnowledgeStore, gateway: ModelGateway) -> None:
        if artifacts.connection is not connection or knowledge.connection is not connection:
            raise GenerationError("generation stores must share one database connection")
        self.connection, self.artifacts, self.knowledge, self.gateway = connection, artifacts, knowledge, gateway
        self.budgets, self.events = BudgetManager(connection), HarnessStore(connection)

    def generate(
        self,
        request: GenerationRequest,
        policy: GenerationPolicy | None = None,
        *,
        forbidden_values: Sequence[str] = (),
        after_model_confirmed: Callable[[str], None] | None = None,
    ) -> dict[str, Any]:
        policy = policy or GenerationPolicy()
        job = self.connection.execute("SELECT status,policy_digest FROM jobs WHERE job_id=?", (request.job_id,)).fetchone()
        if job is None or job["status"] != "RUNNING":
            raise GenerationError("generation requires a RUNNING durable Job")
        persona = self.knowledge.persona_payload(request.persona_snapshot_id)
        allowed_types = persona.get("allowed_question_types")
        if not isinstance(allowed_types, list) or request.question_type not in allowed_types:
            raise GenerationError("persona does not allow the requested question type")
        retrieved = DeterministicRetriever(self.knowledge).retrieve(RetrievalQuery(request.retrieval_query, request.approved_snapshot_ids, policy.retrieval_top_k))
        if not retrieved:
            raise GenerationError("generation retrieval returned no grounded evidence")
        identity = {
            "job_id": request.job_id,
            "seed_id": request.seed_id,
            "seed_hash": digest_text(normalize_text(request.seed_question)),
            "question_type": request.question_type,
            "persona_snapshot_id": request.persona_snapshot_id,
            "knowledge_scope": sorted(set(request.approved_snapshot_ids)),
            "policy_digest": policy.digest,
        }
        generation_id = _hash(identity)
        candidate_id = "cand_" + generation_id[:32]
        revision_id = "rev_" + _hash({"candidate_id": candidate_id, "revision_number": 1})[:32]
        compiler = PromptCompiler(self.knowledge)
        prompt_request = PromptRequest(
            task_mode="GENERATE",
            candidate_revision_id=revision_id,
            task_instruction="Generate exactly one grounded, self-contained, objectively reviewable question. Do not claim it passes review.",
            persona_snapshot_id=request.persona_snapshot_id,
            approved_snapshot_ids=request.approved_snapshot_ids,
            retrieved_chunks=retrieved,
            issue_feedback=(),
            output_schema=GENERATION_OUTPUT_SCHEMA,
            untrusted_context={"seed_id": request.seed_id, "seed_question": normalize_text(request.seed_question), "required_question_type": request.question_type},
        )
        package = compiler.compile(prompt_request, forbidden_values=forbidden_values)
        compiler.persist(package, forbidden_values=forbidden_values)
        self._ensure_run(request, policy, generation_id, candidate_id, revision_id, package.prompt_id)
        row = self._run_row(generation_id)
        if row["status"] in {"COMPLETED", "DUPLICATE"}:
            return self._read_result(row)
        if row["status"] == "ERROR":
            raise GenerationError("generation has a terminal error")
        if row["model_artifact_id"] is None:
            try:
                self._reserve(generation_id, request.job_id, policy.max_tokens)
            except BudgetExceededError:
                self._budget_exhausted(generation_id, candidate_id, request.job_id)
                raise
            try:
                response = self.gateway.complete(ModelRequest(package.system_prompt, package.user_prompt, policy.model, policy.max_tokens, policy.timeout_seconds, generation_id))
            except ModelGatewayError as exc:
                self._release(generation_id, request.job_id, str(exc))
                raise GenerationError(str(exc)) from exc
            leaked = next((secret for secret in forbidden_values if isinstance(secret,str) and len(secret)>=8 and secret in response.text),None)
            if leaked is not None:
                self._consume(generation_id,request.job_id)
                self._terminal_error(generation_id,candidate_id,"model response contained forbidden secret material")
                raise GenerationError("model response contained forbidden secret material")
            with transaction(self.connection):
                artifact = self.artifacts.put_json({"generation_id": generation_id, "text": response.text, "model": response.model, "max_tokens": policy.max_tokens}, kind="generation_model_response", metadata={"generation_id": generation_id})
                self.connection.execute("UPDATE generation_runs SET status='MODEL_CONFIRMED',model_artifact_id=?,provider_response_id=?,updated_at=? WHERE generation_id=?", (artifact.artifact_id,response.response_id,_now(),generation_id))
            if after_model_confirmed:
                after_model_confirmed(generation_id)
        self._consume(generation_id, request.job_id)
        row = self._run_row(generation_id)
        payload = json.loads(self.artifacts.read_bytes(row["model_artifact_id"]))
        try:
            output = self._parse_output(str(payload.get("text", "")), request.question_type, {item.chunk.citation for item in retrieved})
        except GenerationError as exc:
            self._terminal_error(generation_id, candidate_id, str(exc))
            raise
        question_hash = digest_text(normalize_text(output["question"]))
        result = {
            "schema_version": "generation_result.v1",
            "generation_id": generation_id,
            "job_id": request.job_id,
            "candidate_id": candidate_id,
            "revision_id": revision_id,
            "seed_id": request.seed_id,
            "question": output["question"],
            "question_hash": question_hash,
            "question_type": output["question_type"],
            "citations": output["citations"],
            "constraint_coverage": output["constraint_coverage"],
            "generation_rationale": output["generation_rationale"],
            "persona_snapshot_id": request.persona_snapshot_id,
            "knowledge_snapshot_ids": list(sorted(set(request.approved_snapshot_ids))),
            "prompt_id": package.prompt_id,
            "policy_digest": policy.digest,
            "model": policy.model,
            "model_config_hash": _hash({"model": policy.model, "max_tokens": policy.max_tokens}),
            "next_stage": "QUALITY_REVIEW",
            "accepted": False,
        }
        return self._finalize(generation_id, candidate_id, revision_id, result)

    def _ensure_run(self, request: GenerationRequest, policy: GenerationPolicy, generation_id: str, candidate_id: str, revision_id: str, prompt_id: str) -> None:
        now = _now(); model_hash=_hash({"model":policy.model,"max_tokens":policy.max_tokens,"timeout":policy.timeout_seconds})
        with transaction(self.connection):
            source_id = f"{request.seed_id}:{request.persona_snapshot_id}"
            self.connection.execute("INSERT INTO candidates(candidate_id,job_id,source_id,status,created_at,updated_at) VALUES(?,?,?,'GENERATING',?,?) ON CONFLICT(candidate_id) DO NOTHING", (candidate_id,request.job_id,source_id,now,now))
            self.connection.execute("INSERT INTO generation_runs(generation_id,job_id,candidate_id,revision_id,seed_id,persona_snapshot_id,prompt_id,model_config_hash,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(generation_id) DO NOTHING", (generation_id,request.job_id,candidate_id,revision_id,request.seed_id,request.persona_snapshot_id,prompt_id,model_hash,"PENDING",now,now))
            row=self._run_row(generation_id)
            if (row["candidate_id"],row["revision_id"],row["prompt_id"],row["model_config_hash"])!=(candidate_id,revision_id,prompt_id,model_hash):
                raise GenerationError("generation durable identity conflict")
            error_payload=json.loads(row["error_json"]) if row["error_json"] else {}
            if row["status"]=="ERROR" and row["model_artifact_id"] is None and error_payload.get("retryable") is True:
                self.connection.execute("UPDATE generation_runs SET status='PENDING',error_json=NULL,updated_at=? WHERE generation_id=?",(now,generation_id))
                self.connection.execute("UPDATE candidates SET status='GENERATING',updated_at=? WHERE candidate_id=?",(now,candidate_id))

    def _reserve(self, generation_id: str, job_id: str, tokens: int) -> None:
        with transaction(self.connection):
            row=self._run_row(generation_id)
            if row["reserved_tokens"]==0:
                self.budgets.reserve(job_id,"generation_tokens",tokens)
                self.connection.execute("UPDATE generation_runs SET reserved_tokens=?,updated_at=? WHERE generation_id=?",(tokens,_now(),generation_id))

    def _consume(self, generation_id: str, job_id: str) -> None:
        with transaction(self.connection):
            row=self._run_row(generation_id); tokens=int(row["reserved_tokens"])
            if tokens:
                self.budgets.consume(job_id,"generation_tokens",tokens)
                self.connection.execute("UPDATE generation_runs SET reserved_tokens=0,updated_at=? WHERE generation_id=?",(_now(),generation_id))

    def _release(self, generation_id: str, job_id: str, error: str) -> None:
        with transaction(self.connection):
            row=self._run_row(generation_id); tokens=int(row["reserved_tokens"])
            if tokens: self.budgets.release(job_id,"generation_tokens",tokens)
            self.connection.execute("UPDATE generation_runs SET status='ERROR',reserved_tokens=0,error_json=?,updated_at=? WHERE generation_id=?",(canonical_json({"error":error,"retryable":True}),_now(),generation_id))
            self.connection.execute("UPDATE candidates SET status='RETRY_WAIT',updated_at=? WHERE candidate_id=?",(_now(),row["candidate_id"]))

    def _terminal_error(self, generation_id: str, candidate_id: str, error: str) -> None:
        with transaction(self.connection):
            self.connection.execute("UPDATE generation_runs SET status='ERROR',error_json=?,updated_at=? WHERE generation_id=?",(canonical_json({"error":error}),_now(),generation_id))
            self.connection.execute("UPDATE candidates SET status='FAILED',updated_at=? WHERE candidate_id=?",(_now(),candidate_id))

    def _budget_exhausted(self, generation_id: str, candidate_id: str, job_id: str) -> None:
        with transaction(self.connection):
            self.connection.execute("UPDATE generation_runs SET status='ERROR',error_json=?,updated_at=? WHERE generation_id=?",(canonical_json({"error":"BUDGET_EXHAUSTED"}),_now(),generation_id))
            self.connection.execute("UPDATE candidates SET status='BUDGET_EXHAUSTED',updated_at=? WHERE candidate_id=?",(_now(),candidate_id))
            self.events.append_event("candidate",candidate_id,"GENERATION_BUDGET_EXHAUSTED",job_id=job_id,event_key=f"generation:{generation_id}:budget-exhausted")

    def _finalize(self, generation_id: str, candidate_id: str, revision_id: str, result: dict[str, Any]) -> dict[str, Any]:
        now=_now()
        with transaction(self.connection):
            existing=self.connection.execute("SELECT candidate_id FROM generated_question_registry WHERE question_hash=?",(result["question_hash"],)).fetchone()
            if existing is not None and existing["candidate_id"]!=candidate_id:
                duplicate=dict(result,next_stage=None,duplicate_of_candidate_id=str(existing["candidate_id"]),terminal_decision="REJECTED_DUPLICATE")
                artifact=self.artifacts.put_json(duplicate,kind="generation_result",metadata={"generation_id":generation_id})
                self.connection.execute("UPDATE generation_runs SET status='DUPLICATE',result_artifact_id=?,question_hash=?,duplicate_of_candidate_id=?,updated_at=? WHERE generation_id=?",(artifact.artifact_id,result["question_hash"],existing["candidate_id"],now,generation_id))
                self.connection.execute("UPDATE candidates SET status='REJECTED_DUPLICATE',updated_at=? WHERE candidate_id=?",(now,candidate_id))
                self.events.append_event("candidate",candidate_id,"GENERATION_DUPLICATE",job_id=result["job_id"],event_key=f"generation:{generation_id}:duplicate",payload={"question_hash":result["question_hash"]})
                return duplicate
            payload_json=canonical_json({"question":result["question"],"question_type":result["question_type"],"citations":result["citations"],"provenance":{"generation_id":generation_id,"prompt_id":result["prompt_id"]}})
            content_hash=digest_text(payload_json)
            self.connection.execute("INSERT INTO revisions(revision_id,candidate_id,revision_number,content_hash,payload_json,status,created_at) VALUES(?,?,1,?,?,'ACTIVE',?) ON CONFLICT(revision_id) DO NOTHING",(revision_id,candidate_id,content_hash,payload_json,now))
            revision=self.connection.execute("SELECT content_hash,payload_json FROM revisions WHERE revision_id=?",(revision_id,)).fetchone()
            if revision is None or revision["content_hash"]!=content_hash or revision["payload_json"]!=payload_json:
                raise GenerationError("generation revision identity conflict")
            self.connection.execute("INSERT INTO generated_question_registry(question_hash,candidate_id,revision_id,generation_id,created_at) VALUES(?,?,?,?,?) ON CONFLICT(question_hash) DO NOTHING",(result["question_hash"],candidate_id,revision_id,generation_id,now))
            artifact=self.artifacts.put_json(result,kind="generation_result",metadata={"generation_id":generation_id})
            self.connection.execute("UPDATE generation_runs SET status='COMPLETED',result_artifact_id=?,question_hash=?,updated_at=? WHERE generation_id=?",(artifact.artifact_id,result["question_hash"],now,generation_id))
            self.connection.execute("UPDATE candidates SET status='QUALITY_REVIEW',updated_at=? WHERE candidate_id=?",(now,candidate_id))
            self.events.append_event("candidate",candidate_id,"GENERATION_COMPLETED",job_id=result["job_id"],event_key=f"generation:{generation_id}:completed",payload={"revision_id":revision_id,"next_stage":"QUALITY_REVIEW"})
        return result

    @staticmethod
    def _parse_output(text: str, question_type: str, approved_citations: set[str]) -> dict[str, Any]:
        try: value=json.loads(text)
        except json.JSONDecodeError as exc: raise GenerationError("generation output is not strict JSON") from exc
        keys={"question","question_type","citations","constraint_coverage","generation_rationale"}
        if not isinstance(value,dict) or set(value)!=keys: raise GenerationError("generation output keys are invalid")
        question=normalize_text(value["question"]) if isinstance(value["question"],str) else ""
        if not question or len(question)>50_000 or value["question_type"]!=question_type: raise GenerationError("generation question or type is invalid")
        citations=value["citations"]
        if not isinstance(citations,list) or not citations or len(set(citations))!=len(citations) or not set(citations)<=approved_citations: raise GenerationError("generation citations are invalid")
        coverage=value["constraint_coverage"]
        if not isinstance(coverage,list) or not coverage or not all(isinstance(x,str) and x.strip() for x in coverage): raise GenerationError("generation constraint coverage is invalid")
        rationale=value["generation_rationale"]
        if not isinstance(rationale,str) or not rationale.strip(): raise GenerationError("generation rationale is invalid")
        return {"question":question,"question_type":question_type,"citations":citations,"constraint_coverage":[x.strip() for x in coverage],"generation_rationale":rationale.strip()}

    def _run_row(self, generation_id: str) -> sqlite3.Row:
        row=self.connection.execute("SELECT * FROM generation_runs WHERE generation_id=?",(generation_id,)).fetchone()
        if row is None: raise GenerationError("generation run is missing")
        return row

    def _read_result(self, row: sqlite3.Row) -> dict[str, Any]:
        if row["result_artifact_id"] is None: raise GenerationError("terminal generation result Artifact is missing")
        value=json.loads(self.artifacts.read_bytes(row["result_artifact_id"]))
        if not isinstance(value,dict): raise GenerationError("generation result Artifact is invalid")
        return value
