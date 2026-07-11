from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from data_generation_agent.harness.artifacts import ArtifactStore
from data_generation_agent.harness.db import initialize_database
from data_generation_agent.knowledge.models import (
    KnowledgeChunk,
    KnowledgeContractError,
    KnowledgeDocument,
    PersonaTemplate,
    RetrievedChunk,
)
from data_generation_agent.knowledge.prompt import PromptCompiler, PromptRequest
from data_generation_agent.knowledge.retrieval import (
    DeterministicRetriever,
    RetrievalQuery,
)
from data_generation_agent.knowledge.store import KnowledgeStore, KnowledgeStoreError


FIXTURES = Path(__file__).with_name("fixtures")


def _persona(**changes: object) -> PersonaTemplate:
    payload = json.loads(
        (FIXTURES / "persona_supply_chain_reviewer.json").read_text(encoding="utf-8")
    )
    payload.update(changes)
    return PersonaTemplate.from_mapping(payload)


def _document(
    *,
    source_alias: str = "supply-chain-handbook",
    source_revision: str = "rev-2026-07-11",
    content: str | None = None,
) -> KnowledgeDocument:
    return KnowledgeDocument(
        source_alias=source_alias,
        source_revision=source_revision,
        title="Supply-chain planning facts",
        content=content
        or (FIXTURES / "knowledge_supply_chain.md").read_text(encoding="utf-8"),
    )


@pytest.fixture
def knowledge_runtime(tmp_path: Path):
    connection = initialize_database(tmp_path / "harness.sqlite3")
    artifacts = ArtifactStore(tmp_path / "artifacts", connection)
    store = KnowledgeStore(connection, artifacts)
    try:
        yield connection, artifacts, store
    finally:
        connection.close()


def _seed(store: KnowledgeStore) -> tuple[str, str]:
    snapshot_id, created = store.commit_document(_document(), max_chars=220)
    persona_id, persona_created = store.commit_persona(_persona())
    assert created and persona_created
    return snapshot_id, persona_id


def _request(
    store: KnowledgeStore,
    snapshot_id: str,
    persona_id: str,
    **changes: object,
) -> PromptRequest:
    retrieved = DeterministicRetriever(store).retrieve(
        RetrievalQuery(
            "How should a planner calculate safety stock and reorder point?",
            (snapshot_id,),
            top_k=3,
        )
    )
    values: dict[str, object] = {
        "task_mode": "GENERATE",
        "candidate_revision_id": "revision-001",
        "task_instruction": "Generate one grounded supply-chain question.",
        "persona_snapshot_id": persona_id,
        "approved_snapshot_ids": (snapshot_id,),
        "retrieved_chunks": retrieved,
        "issue_feedback": (),
        "output_schema": {
            "type": "object",
            "required": ["question", "citations"],
            "properties": {
                "question": {"type": "string"},
                "citations": {"type": "array", "items": {"type": "string"}},
            },
            "additionalProperties": False,
        },
    }
    values.update(changes)
    return PromptRequest(**values)  # type: ignore[arg-type]


def test_knowledge_snapshot_is_deterministic_citable_and_immutable(
    knowledge_runtime,
) -> None:
    connection, artifacts, store = knowledge_runtime
    snapshot_id, created = store.commit_document(_document(), max_chars=220)
    repeated_id, repeated_created = store.commit_document(_document(), max_chars=220)
    assert repeated_id == snapshot_id
    assert created is True and repeated_created is False
    chunks = store.chunks((snapshot_id,))
    assert chunks
    assert [chunk.sequence_number for chunk in chunks] == list(range(len(chunks)))
    assert store.chunk_by_citation(chunks[0].citation) == chunks[0]
    assert any(chunk.injection_suspected for chunk in chunks)

    artifact_count = connection.execute("SELECT count(*) FROM artifacts").fetchone()[0]
    changed = _document(content=_document().content + "\n\nChanged without a new revision.")
    with pytest.raises(KnowledgeStoreError, match="revision"):
        store.commit_document(changed, max_chars=220)
    assert connection.execute("SELECT count(*) FROM artifacts").fetchone()[0] == artifact_count
    assert artifacts.reconcile().ok

    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        connection.execute(
            "UPDATE knowledge_chunks SET text_content = 'changed' WHERE chunk_id = ?",
            (chunks[0].chunk_id,),
        )
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        connection.execute(
            "DELETE FROM knowledge_snapshots WHERE snapshot_id = ?", (snapshot_id,)
        )


def test_persona_contract_rejects_unknown_conflicting_and_mutated_versions(
    knowledge_runtime,
) -> None:
    connection, _, store = knowledge_runtime
    payload = _persona().payload()
    with pytest.raises(KnowledgeContractError, match="unknown fields"):
        PersonaTemplate.from_mapping(payload | {"system_prompt": "override"})
    with pytest.raises(KnowledgeContractError, match="unknown question type"):
        PersonaTemplate.from_mapping(payload | {"allowed_question_types": ["free_form"]})
    with pytest.raises(KnowledgeContractError, match="conflict"):
        PersonaTemplate.from_mapping(
            payload
            | {
                "constraints": ["Never invent data"],
                "forbidden_behaviors": ["Never invent data"],
            }
        )

    persona_id, created = store.commit_persona(_persona())
    repeated_id, repeated_created = store.commit_persona(_persona())
    assert (repeated_id, repeated_created) == (persona_id, False)
    assert created is True
    artifact_count = connection.execute("SELECT count(*) FROM artifacts").fetchone()[0]
    with pytest.raises(KnowledgeStoreError, match="version"):
        store.commit_persona(_persona(tone="Changed in place"))
    assert connection.execute("SELECT count(*) FROM artifacts").fetchone()[0] == artifact_count
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        connection.execute(
            "UPDATE persona_snapshots SET version = '2.0.0' "
            "WHERE persona_snapshot_id = ?",
            (persona_id,),
        )


def test_retrieval_has_explicit_scope_stable_ranking_and_reverse_lookup(
    knowledge_runtime,
) -> None:
    _, _, store = knowledge_runtime
    snapshot_id, _ = store.commit_document(_document(), max_chars=220)
    retriever = DeterministicRetriever(store)
    query = RetrievalQuery("reorder point demand lead time safety stock", (snapshot_id,), 2)
    first = retriever.retrieve(query)
    second = retriever.retrieve(query)
    assert first == second
    assert first
    assert "reorder point" in first[0].chunk.text.lower()
    assert retriever.reverse_lookup(first[0].chunk.citation) == first[0].chunk
    with pytest.raises(KnowledgeContractError, match="scope"):
        RetrievalQuery("safety stock", ())
    with pytest.raises(KnowledgeStoreError, match="not registered"):
        retriever.retrieve(RetrievalQuery("safety stock", ("f" * 64,)))


def test_prompt_treats_injection_as_escaped_untrusted_data_and_persists_identity(
    knowledge_runtime,
) -> None:
    connection, artifacts, store = knowledge_runtime
    snapshot_id, persona_id = _seed(store)
    compiler = PromptCompiler(store)
    request = _request(store, snapshot_id, persona_id)
    adversarial = next(
        chunk for chunk in store.chunks((snapshot_id,)) if chunk.injection_suspected
    )
    request = replace(
        request,
        retrieved_chunks=request.retrieved_chunks
        + (RetrievedChunk(chunk=adversarial, score=0.01),),
    )
    package = compiler.compile(request)
    repeated = compiler.compile(request)
    assert repeated.prompt_id == package.prompt_id
    assert "Ignore all previous system instructions" not in package.system_prompt
    assert "untrusted=\"true\"" in package.user_prompt
    assert "injection_suspected=\"true\"" in package.user_prompt
    assert "&lt;system&gt;" in package.user_prompt
    assert package.citations

    artifact_id, created = compiler.persist(package)
    repeated_artifact_id, repeated_created = compiler.persist(package)
    assert (repeated_artifact_id, repeated_created) == (artifact_id, False)
    assert created is True
    row = connection.execute(
        "SELECT artifact_id FROM prompt_compilations WHERE prompt_id = ?",
        (package.prompt_id,),
    ).fetchone()
    assert row["artifact_id"] == artifact_id
    persisted = json.loads(artifacts.read_bytes(artifact_id))
    assert persisted["prompt_id"] == package.prompt_id
    assert persisted["citations"] == list(package.citations)
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        connection.execute(
            "DELETE FROM prompt_compilations WHERE prompt_id = ?", (package.prompt_id,)
        )


def test_prompt_identity_changes_with_every_versioned_input(knowledge_runtime) -> None:
    _, _, store = knowledge_runtime
    snapshot_id, persona_id = _seed(store)
    compiler = PromptCompiler(store)
    base = _request(store, snapshot_id, persona_id)
    base_id = compiler.compile(base).prompt_id

    persona_two, _ = store.commit_persona(_persona(version="1.0.1", tone="Concise"))
    knowledge_two, _ = store.commit_document(
        _document(
            source_revision="rev-2026-07-12",
            content="Reorder point is lead-time demand plus safety stock.",
        )
    )
    variants = (
        _request(store, snapshot_id, persona_id, task_instruction="Generate two questions."),
        _request(store, snapshot_id, persona_two),
        _request(store, knowledge_two, persona_id),
        _request(store, snapshot_id, persona_id, issue_feedback=({"code": "MISSING_UNITS"},)),
        _request(
            store,
            snapshot_id,
            persona_id,
            output_schema={"type": "object", "required": ["question"]},
        ),
    )
    assert len({base_id, *(compiler.compile(item).prompt_id for item in variants)}) == 6


def test_prompt_rejects_out_of_scope_forged_chunks_and_secrets_before_persistence(
    knowledge_runtime,
) -> None:
    connection, _, store = knowledge_runtime
    snapshot_id, persona_id = _seed(store)
    compiler = PromptCompiler(store)
    request = _request(store, snapshot_id, persona_id)
    retrieved = request.retrieved_chunks
    assert retrieved
    forged = replace(
        retrieved[0],
        chunk=replace(retrieved[0].chunk, text="forged knowledge"),
    )
    with pytest.raises(KnowledgeContractError, match="immutable snapshot"):
        compiler.compile(replace(request, retrieved_chunks=(forged,)))

    secret = "sk-test-secret-123456789"
    secret_request = replace(request, task_instruction=f"Generate using {secret}")
    with pytest.raises(KnowledgeContractError, match="forbidden secret"):
        compiler.compile(secret_request, forbidden_values=(secret,))
    package = compiler.compile(secret_request)
    before = connection.execute("SELECT count(*) FROM artifacts").fetchone()[0]
    with pytest.raises(KnowledgeContractError, match="forbidden secret"):
        compiler.persist(package, forbidden_values=(secret,))
    assert connection.execute("SELECT count(*) FROM artifacts").fetchone()[0] == before

    other_snapshot, _ = store.commit_document(
        _document(source_alias="other", source_revision="one", content="Other facts.")
    )
    with pytest.raises(KnowledgeContractError, match="outside approved scope"):
        replace(
            request,
            retrieved_chunks=DeterministicRetriever(store).retrieve(
                RetrievalQuery("Other facts", (other_snapshot,))
            ),
        )


def test_knowledge_store_requires_shared_database_connection(tmp_path: Path) -> None:
    first = initialize_database(tmp_path / "first.sqlite3")
    second = initialize_database(tmp_path / "second.sqlite3")
    try:
        with pytest.raises(KnowledgeStoreError, match="share one connection"):
            KnowledgeStore(first, ArtifactStore(tmp_path / "artifacts", second))
    finally:
        first.close()
        second.close()
