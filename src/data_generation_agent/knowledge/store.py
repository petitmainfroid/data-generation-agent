from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from data_generation_agent.harness.artifacts import ArtifactStore
from data_generation_agent.harness.db import transaction

from .models import (
    KnowledgeChunk,
    KnowledgeContractError,
    KnowledgeDocument,
    PersonaTemplate,
    canonical_json,
    digest_text,
    injection_suspected,
    retrieval_tokens,
)


class KnowledgeStoreError(RuntimeError):
    """An immutable knowledge/persona snapshot could not be committed."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


def chunk_document(document: KnowledgeDocument, *, max_chars: int = 1200) -> tuple[str, ...]:
    if isinstance(max_chars, bool) or not isinstance(max_chars, int) or max_chars < 100:
        raise KnowledgeContractError("max_chars must be an integer of at least 100")
    paragraphs = [part.strip() for part in document.content.split("\n\n") if part.strip()]
    chunks: list[str] = []
    current = ""
    for paragraph in paragraphs:
        pieces = [
            paragraph[index : index + max_chars]
            for index in range(0, len(paragraph), max_chars)
        ] or [paragraph]
        for piece in pieces:
            candidate = piece if not current else f"{current}\n\n{piece}"
            if len(candidate) <= max_chars:
                current = candidate
            else:
                if current:
                    chunks.append(current)
                current = piece
    if current:
        chunks.append(current)
    if not chunks:
        raise KnowledgeContractError("document produced no knowledge chunks")
    return tuple(chunks)


class KnowledgeStore:
    def __init__(self, connection: sqlite3.Connection, artifacts: ArtifactStore) -> None:
        if artifacts.connection is not connection:
            raise KnowledgeStoreError("knowledge and artifacts must share one connection")
        self.connection = connection
        self.artifacts = artifacts

    def commit_document(
        self, document: KnowledgeDocument, *, max_chars: int = 1200
    ) -> tuple[str, bool]:
        chunk_texts = chunk_document(document, max_chars=max_chars)
        content_hash = digest_text(document.content)
        snapshot_material = canonical_json(
            {
                "source_alias": document.source_alias,
                "source_revision": document.source_revision,
                "title": document.title,
                "content_hash": content_hash,
            }
        )
        snapshot_id = digest_text(snapshot_material)
        chunks: list[KnowledgeChunk] = []
        for sequence, text in enumerate(chunk_texts):
            text_hash = digest_text(text)
            chunk_id = digest_text(f"{snapshot_id}:{sequence}:{text_hash}")
            chunks.append(
                KnowledgeChunk(
                    chunk_id=chunk_id,
                    snapshot_id=snapshot_id,
                    sequence_number=sequence,
                    citation=f"{document.source_alias}@{document.source_revision}#chunk-{sequence + 1}",
                    text=text,
                    text_hash=text_hash,
                    tokens=retrieval_tokens(text),
                    injection_suspected=injection_suspected(text),
                )
            )
        payload = {
            "schema_version": "knowledge_snapshot.v1",
            "snapshot_id": snapshot_id,
            "source_alias": document.source_alias,
            "source_revision": document.source_revision,
            "title": document.title,
            "content_hash": content_hash,
            "chunks": [
                {
                    "chunk_id": chunk.chunk_id,
                    "sequence_number": chunk.sequence_number,
                    "citation": chunk.citation,
                    "text": chunk.text,
                    "text_hash": chunk.text_hash,
                    "injection_suspected": chunk.injection_suspected,
                }
                for chunk in chunks
            ],
        }
        timestamp = _now()
        with transaction(self.connection, mode="IMMEDIATE"):
            existing_revision = self.connection.execute(
                "SELECT snapshot_id, content_hash FROM knowledge_snapshots "
                "WHERE source_alias = ? AND source_revision = ?",
                (document.source_alias, document.source_revision),
            ).fetchone()
            if existing_revision is not None and (
                existing_revision["snapshot_id"] != snapshot_id
                or existing_revision["content_hash"] != content_hash
            ):
                raise KnowledgeStoreError(
                    "knowledge source revision already identifies different content"
                )
            artifact = self.artifacts.put_json(
                payload,
                kind="knowledge_snapshot",
                metadata={"snapshot_id": snapshot_id, "source_alias": document.source_alias},
            )
            inserted = self.connection.execute(
                """
                INSERT INTO knowledge_snapshots(
                    snapshot_id, source_alias, source_revision, title, content_hash,
                    artifact_id, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(snapshot_id) DO NOTHING
                """,
                (
                    snapshot_id,
                    document.source_alias,
                    document.source_revision,
                    document.title,
                    content_hash,
                    artifact.artifact_id,
                    timestamp,
                ),
            ).rowcount
            row = self.connection.execute(
                "SELECT artifact_id, content_hash FROM knowledge_snapshots WHERE snapshot_id = ?",
                (snapshot_id,),
            ).fetchone()
            if row is None or row["artifact_id"] != artifact.artifact_id or row[
                "content_hash"
            ] != content_hash:
                raise KnowledgeStoreError("knowledge snapshot identity conflict")
            for chunk in chunks:
                chunk_inserted = self.connection.execute(
                    """
                    INSERT INTO knowledge_chunks(
                        chunk_id, snapshot_id, sequence_number, citation, text_hash,
                        text_content, token_json, injection_suspected, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(chunk_id) DO NOTHING
                    """,
                    (
                        chunk.chunk_id,
                        snapshot_id,
                        chunk.sequence_number,
                        chunk.citation,
                        chunk.text_hash,
                        chunk.text,
                        canonical_json(list(chunk.tokens)),
                        int(chunk.injection_suspected),
                        timestamp,
                    ),
                ).rowcount
                if not chunk_inserted:
                    row = self.connection.execute(
                        "SELECT snapshot_id, sequence_number, citation, text_hash, "
                        "text_content FROM knowledge_chunks WHERE chunk_id = ?",
                        (chunk.chunk_id,),
                    ).fetchone()
                    if row is None or (
                        row["snapshot_id"] != snapshot_id
                        or row["sequence_number"] != chunk.sequence_number
                        or row["citation"] != chunk.citation
                        or row["text_hash"] != chunk.text_hash
                        or row["text_content"] != chunk.text
                    ):
                        raise KnowledgeStoreError("knowledge chunk identity conflict")
        return snapshot_id, bool(inserted)

    def commit_persona(self, persona: PersonaTemplate) -> tuple[str, bool]:
        payload = persona.payload()
        payload_json = canonical_json(payload)
        snapshot_id = digest_text(payload_json)
        if snapshot_id != persona.snapshot_id:
            raise KnowledgeStoreError("persona snapshot identity mismatch")
        timestamp = _now()
        with transaction(self.connection, mode="IMMEDIATE"):
            existing_version = self.connection.execute(
                "SELECT persona_snapshot_id, payload_hash FROM persona_snapshots "
                "WHERE persona_id = ? AND version = ?",
                (persona.persona_id, persona.version),
            ).fetchone()
            if existing_version is not None and (
                existing_version["persona_snapshot_id"] != snapshot_id
                or existing_version["payload_hash"] != snapshot_id
            ):
                raise KnowledgeStoreError(
                    "persona version already identifies a different template"
                )
            artifact = self.artifacts.put_json(
                payload,
                kind="persona_snapshot",
                metadata={"persona_id": persona.persona_id, "version": persona.version},
            )
            inserted = self.connection.execute(
                """
                INSERT INTO persona_snapshots(
                    persona_snapshot_id, persona_id, version, payload_hash,
                    artifact_id, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(persona_snapshot_id) DO NOTHING
                """,
                (
                    snapshot_id,
                    persona.persona_id,
                    persona.version,
                    snapshot_id,
                    artifact.artifact_id,
                    timestamp,
                ),
            ).rowcount
            row = self.connection.execute(
                "SELECT artifact_id, payload_hash FROM persona_snapshots "
                "WHERE persona_snapshot_id = ?",
                (snapshot_id,),
            ).fetchone()
            if row is None or row["artifact_id"] != artifact.artifact_id or row[
                "payload_hash"
            ] != snapshot_id:
                raise KnowledgeStoreError("persona snapshot identity conflict")
        return snapshot_id, bool(inserted)

    def chunks(self, snapshot_ids: Iterable[str]) -> tuple[KnowledgeChunk, ...]:
        approved = tuple(sorted(set(snapshot_ids)))
        if not approved:
            return ()
        placeholders = ",".join("?" for _ in approved)
        rows = self.connection.execute(
            f"SELECT * FROM knowledge_chunks WHERE snapshot_id IN ({placeholders}) "
            "ORDER BY snapshot_id, sequence_number",
            approved,
        ).fetchall()
        found_snapshots = {str(row["snapshot_id"]) for row in rows}
        missing = set(approved) - found_snapshots
        if missing:
            raise KnowledgeStoreError("one or more knowledge snapshots are not registered")
        return tuple(self._chunk(row) for row in rows)

    def chunk_by_citation(self, citation: str) -> KnowledgeChunk:
        row = self.connection.execute(
            "SELECT * FROM knowledge_chunks WHERE citation = ?", (citation,)
        ).fetchone()
        if row is None:
            raise KnowledgeStoreError("citation is not registered")
        return self._chunk(row)

    def persona_payload(self, snapshot_id: str) -> dict[str, object]:
        row = self.connection.execute(
            "SELECT artifact_id FROM persona_snapshots WHERE persona_snapshot_id = ?",
            (snapshot_id,),
        ).fetchone()
        if row is None:
            raise KnowledgeStoreError("persona snapshot is not registered")
        payload = json.loads(self.artifacts.read_bytes(row["artifact_id"]).decode("utf-8"))
        if not isinstance(payload, dict):
            raise KnowledgeStoreError("persona artifact is invalid")
        return payload

    @staticmethod
    def _chunk(row: sqlite3.Row) -> KnowledgeChunk:
        tokens = json.loads(row["token_json"])
        return KnowledgeChunk(
            chunk_id=str(row["chunk_id"]),
            snapshot_id=str(row["snapshot_id"]),
            sequence_number=int(row["sequence_number"]),
            citation=str(row["citation"]),
            text=str(row["text_content"]),
            text_hash=str(row["text_hash"]),
            tokens=tuple(str(token) for token in tokens),
            injection_suspected=bool(row["injection_suspected"]),
        )
