from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest

from data_generation_agent.harness.artifacts import (
    ArtifactIntegrityError,
    ArtifactStore,
    ArtifactStoreError,
)
from data_generation_agent.harness.db import initialize_database


@pytest.fixture
def artifact_store(tmp_path: Path):
    connection = initialize_database(tmp_path / "harness.sqlite3")
    store = ArtifactStore(tmp_path / "artifacts", connection)
    try:
        yield store
    finally:
        connection.close()


def test_content_addressed_put_is_durable_and_reused_without_overwrite(
    artifact_store: ArtifactStore,
) -> None:
    payload = b"immutable model response\n"
    expected_hash = hashlib.sha256(payload).hexdigest()

    first = artifact_store.put_bytes(
        payload,
        kind="model_response",
        media_type="application/json",
        metadata={"attempt": 1},
    )
    path = artifact_store.resolve_uri(first.uri)
    first_stat = path.stat()
    second = artifact_store.put_bytes(
        payload,
        kind="different_kind_is_not_an_overwrite",
        metadata={"attempt": 2},
    )

    assert first == second
    assert first.artifact_id == f"sha256:{expected_hash}"
    assert first.sha256 == expected_hash
    assert first.uri == ArtifactStore.uri_for(expected_hash)
    assert artifact_store.read_bytes(first.artifact_id) == payload
    assert path.stat().st_mtime_ns == first_stat.st_mtime_ns
    assert artifact_store.connection.execute("SELECT count(*) FROM artifacts").fetchone()[0] == 1


def test_atomic_write_failure_leaves_no_row_or_partial_file(
    artifact_store: ArtifactStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = b"must not become partial"
    digest = hashlib.sha256(payload).hexdigest()
    destination = artifact_store.resolve_uri(ArtifactStore.uri_for(digest))

    def fail_replace(source: os.PathLike[str], target: os.PathLike[str]) -> None:
        raise OSError("injected rename failure")

    monkeypatch.setattr("data_generation_agent.harness.artifacts.os.replace", fail_replace)
    with pytest.raises(OSError, match="injected rename failure"):
        artifact_store.put_bytes(payload, kind="fault_injection")

    assert not destination.exists()
    assert artifact_store.connection.execute("SELECT count(*) FROM artifacts").fetchone()[0] == 0
    assert not list(artifact_store.root.rglob(".tmp-*"))


def test_path_containment_rejects_absolute_and_parent_uris(
    artifact_store: ArtifactStore, tmp_path: Path
) -> None:
    with pytest.raises(ArtifactStoreError, match="escapes"):
        artifact_store.resolve_uri("../outside.bin")
    with pytest.raises(ArtifactStoreError, match="relative"):
        artifact_store.resolve_uri(str((tmp_path / "outside.bin").resolve()))


def test_reconcile_adopts_orphans_and_reports_missing_and_corrupt_files(
    artifact_store: ArtifactStore,
) -> None:
    orphan_payload = b"durable before database commit"
    orphan_hash = hashlib.sha256(orphan_payload).hexdigest()
    orphan_path = artifact_store.resolve_uri(ArtifactStore.uri_for(orphan_hash))
    orphan_path.parent.mkdir(parents=True, exist_ok=True)
    orphan_path.write_bytes(orphan_payload)

    adopted = artifact_store.reconcile()
    orphan_id = f"sha256:{orphan_hash}"
    assert adopted.inserted_orphans == (orphan_id,)
    assert adopted.ok is True
    assert artifact_store.read_bytes(orphan_id) == orphan_payload

    missing = artifact_store.put_text("later missing", kind="test")
    artifact_store.resolve_uri(missing.uri).unlink()
    corrupt = artifact_store.put_text("original", kind="test")
    artifact_store.resolve_uri(corrupt.uri).write_bytes(b"tampered")
    (artifact_store.root / "unexpected.txt").write_text("unexpected", encoding="utf-8")

    report = artifact_store.reconcile()
    assert missing.artifact_id in report.missing_files
    assert corrupt.artifact_id in report.corrupt_files
    assert "unexpected.txt" in report.unexpected_files
    assert report.ok is False
    with pytest.raises(ArtifactIntegrityError):
        artifact_store.read_bytes(corrupt.artifact_id)


def test_json_artifact_is_canonical_across_key_order(artifact_store: ArtifactStore) -> None:
    first = artifact_store.put_json({"b": 2, "a": 1}, kind="summary")
    second = artifact_store.put_json({"a": 1, "b": 2}, kind="summary")
    assert first.artifact_id == second.artifact_id
    assert artifact_store.read_bytes(first.artifact_id) == b'{"a":1,"b":2}\n'
