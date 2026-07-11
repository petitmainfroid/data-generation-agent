from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
import threading
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping

from .db import transaction


class ArtifactStoreError(RuntimeError):
    """The immutable artifact contract could not be satisfied."""


class ArtifactIntegrityError(ArtifactStoreError):
    """Artifact bytes, their address, and their database record disagree."""


@dataclass(frozen=True)
class ArtifactRecord:
    artifact_id: str
    sha256: str
    uri: str
    kind: str
    media_type: str
    size_bytes: int
    metadata: dict[str, Any]
    created_at: str
    job_id: str | None = None
    revision_id: str | None = None
    attempt_id: str | None = None


@dataclass(frozen=True)
class ArtifactReconcileReport:
    scanned_files: int
    database_records: int
    inserted_orphans: tuple[str, ...]
    missing_files: tuple[str, ...]
    corrupt_files: tuple[str, ...]
    unexpected_files: tuple[str, ...]
    path_violations: tuple[str, ...]
    temporary_files: tuple[str, ...]
    database_conflicts: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return not (
            self.missing_files
            or self.corrupt_files
            or self.unexpected_files
            or self.path_violations
            or self.database_conflicts
        )

    def as_dict(self) -> dict[str, Any]:
        return asdict(self) | {"ok": self.ok}


def _utc_now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


class ArtifactStore:
    """A local, content-addressed store backed by the Harness artifact table.

    Bytes are durable before their database row is committed. If the process
    dies between those operations, :meth:`reconcile` adopts the orphaned file.
    Database rows are never updated; the migration enforces that invariant too.
    """

    _write_lock = threading.RLock()

    def __init__(self, root: Path, connection: sqlite3.Connection) -> None:
        root.mkdir(parents=True, exist_ok=True)
        if not root.is_dir():
            raise ArtifactStoreError(f"artifact root is not a directory: {root}")
        self.root = root.resolve()
        self.connection = connection

    @staticmethod
    def artifact_id_for(digest: str) -> str:
        return f"sha256:{digest}"

    @staticmethod
    def uri_for(digest: str) -> str:
        if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
            raise ArtifactStoreError(f"invalid sha256 digest: {digest}")
        return f"sha256/{digest[:2]}/{digest[2:4]}/{digest}"

    def resolve_uri(self, uri: str) -> Path:
        raw = Path(uri)
        if raw.is_absolute():
            raise ArtifactStoreError(f"artifact URI must be relative: {uri}")
        resolved = (self.root / raw).resolve()
        try:
            resolved.relative_to(self.root)
        except ValueError as exc:
            raise ArtifactStoreError(f"artifact URI escapes the store root: {uri}") from exc
        return resolved

    def put_bytes(
        self,
        data: bytes | bytearray | memoryview,
        *,
        kind: str,
        media_type: str = "application/octet-stream",
        metadata: Mapping[str, Any] | None = None,
        job_id: str | None = None,
        revision_id: str | None = None,
        attempt_id: str | None = None,
    ) -> ArtifactRecord:
        if not isinstance(data, (bytes, bytearray, memoryview)):
            raise TypeError("artifact data must be bytes-like")
        if not kind.strip():
            raise ArtifactStoreError("artifact kind must not be empty")
        if not media_type.strip():
            raise ArtifactStoreError("artifact media_type must not be empty")
        payload = bytes(data)
        metadata_value = dict(metadata or {})
        try:
            metadata_json = json.dumps(
                metadata_value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        except (TypeError, ValueError) as exc:
            raise ArtifactStoreError("artifact metadata must be JSON serializable") from exc

        digest = _digest(payload)
        existing = self._get_by_sha256(digest)
        uri = existing.uri if existing is not None else self.uri_for(digest)
        destination = self.resolve_uri(uri)
        if existing is not None and destination.exists():
            self._assert_integrity(destination, digest, len(payload))
            return existing

        self._write_immutable(destination, payload, digest)
        created_at = _utc_now()
        try:
            with transaction(self.connection):
                self.connection.execute(
                    """
                    INSERT INTO artifacts(
                        artifact_id, job_id, revision_id, attempt_id, kind,
                        sha256, uri, media_type, size_bytes, metadata_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(sha256) DO NOTHING
                    """,
                    (
                        self.artifact_id_for(digest),
                        job_id,
                        revision_id,
                        attempt_id,
                        kind,
                        digest,
                        uri,
                        media_type,
                        len(payload),
                        metadata_json,
                        created_at,
                    ),
                )
        except sqlite3.DatabaseError as exc:
            # The durable file intentionally remains for reconcile() to adopt.
            raise ArtifactStoreError(f"failed to register artifact {digest}") from exc

        record = self._get_by_sha256(digest)
        if record is None:
            raise ArtifactStoreError(f"artifact row missing after insert: {digest}")
        if record.uri != uri or record.size_bytes != len(payload):
            raise ArtifactIntegrityError(f"conflicting database record for artifact: {digest}")
        return record

    def put_text(
        self,
        text: str,
        *,
        kind: str,
        media_type: str = "text/plain; charset=utf-8",
        metadata: Mapping[str, Any] | None = None,
        job_id: str | None = None,
        revision_id: str | None = None,
        attempt_id: str | None = None,
    ) -> ArtifactRecord:
        return self.put_bytes(
            text.encode("utf-8"),
            kind=kind,
            media_type=media_type,
            metadata=metadata,
            job_id=job_id,
            revision_id=revision_id,
            attempt_id=attempt_id,
        )

    def put_json(
        self,
        value: Any,
        *,
        kind: str,
        metadata: Mapping[str, Any] | None = None,
        job_id: str | None = None,
        revision_id: str | None = None,
        attempt_id: str | None = None,
    ) -> ArtifactRecord:
        try:
            data = (
                json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                + "\n"
            ).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise ArtifactStoreError("artifact value must be JSON serializable") from exc
        return self.put_bytes(
            data,
            kind=kind,
            media_type="application/json",
            metadata=metadata,
            job_id=job_id,
            revision_id=revision_id,
            attempt_id=attempt_id,
        )

    def get(self, artifact_id: str) -> ArtifactRecord | None:
        row = self.connection.execute(
            "SELECT * FROM artifacts WHERE artifact_id = ?", (artifact_id,)
        ).fetchone()
        return self._record(row) if row is not None else None

    def read_bytes(self, artifact_id: str) -> bytes:
        record = self.get(artifact_id)
        if record is None:
            raise ArtifactStoreError(f"artifact is not registered: {artifact_id}")
        path = self.resolve_uri(record.uri)
        if not path.is_file():
            raise ArtifactIntegrityError(f"artifact file is missing: {artifact_id}")
        self._assert_integrity(path, record.sha256, record.size_bytes)
        return path.read_bytes()

    def reconcile(self) -> ArtifactReconcileReport:
        inserted: list[str] = []
        corrupt: list[str] = []
        unexpected: list[str] = []
        path_violations: list[str] = []
        temporary: list[str] = []
        conflicts: list[str] = []
        scanned = 0

        for path in sorted(item for item in self.root.rglob("*") if item.is_file()):
            relative = path.relative_to(self.root).as_posix()
            if path.name.startswith(".tmp-") or path.name.endswith(".tmp"):
                temporary.append(relative)
                continue
            scanned += 1
            digest = _file_digest(path)
            expected_uri = self.uri_for(digest)
            if relative != expected_uri:
                unexpected.append(relative)
                continue
            record = self._get_by_sha256(digest)
            if record is not None:
                if record.uri != relative or record.size_bytes != path.stat().st_size:
                    corrupt.append(record.artifact_id)
                continue
            try:
                with transaction(self.connection):
                    self.connection.execute(
                        """
                        INSERT INTO artifacts(
                            artifact_id, job_id, revision_id, attempt_id, kind,
                            sha256, uri, media_type, size_bytes, metadata_json, created_at
                        ) VALUES (?, NULL, NULL, NULL, 'reconciled', ?, ?,
                                  'application/octet-stream', ?, ?, ?)
                        """,
                        (
                            self.artifact_id_for(digest),
                            digest,
                            relative,
                            path.stat().st_size,
                            '{"reconciled":true}',
                            _utc_now(),
                        ),
                    )
                inserted.append(self.artifact_id_for(digest))
            except sqlite3.IntegrityError:
                conflicts.append(relative)

        rows = self.connection.execute("SELECT * FROM artifacts ORDER BY artifact_id").fetchall()
        missing: list[str] = []
        for row in rows:
            record = self._record(row)
            try:
                path = self.resolve_uri(record.uri)
            except ArtifactStoreError:
                path_violations.append(record.artifact_id)
                continue
            if not path.is_file():
                missing.append(record.artifact_id)
                continue
            try:
                self._assert_integrity(path, record.sha256, record.size_bytes)
            except ArtifactIntegrityError:
                corrupt.append(record.artifact_id)

        return ArtifactReconcileReport(
            scanned_files=scanned,
            database_records=len(rows),
            inserted_orphans=tuple(sorted(set(inserted))),
            missing_files=tuple(sorted(set(missing))),
            corrupt_files=tuple(sorted(set(corrupt))),
            unexpected_files=tuple(sorted(set(unexpected))),
            path_violations=tuple(sorted(set(path_violations))),
            temporary_files=tuple(sorted(set(temporary))),
            database_conflicts=tuple(sorted(set(conflicts))),
        )

    def _get_by_sha256(self, digest: str) -> ArtifactRecord | None:
        row = self.connection.execute(
            "SELECT * FROM artifacts WHERE sha256 = ?", (digest,)
        ).fetchone()
        return self._record(row) if row is not None else None

    @staticmethod
    def _record(row: sqlite3.Row) -> ArtifactRecord:
        metadata = json.loads(row["metadata_json"])
        if not isinstance(metadata, dict):
            raise ArtifactIntegrityError(f"artifact metadata is not an object: {row['artifact_id']}")
        return ArtifactRecord(
            artifact_id=row["artifact_id"],
            sha256=row["sha256"],
            uri=row["uri"],
            kind=row["kind"],
            media_type=row["media_type"],
            size_bytes=row["size_bytes"],
            metadata=metadata,
            created_at=row["created_at"],
            job_id=row["job_id"],
            revision_id=row["revision_id"],
            attempt_id=row["attempt_id"],
        )

    def _write_immutable(self, destination: Path, payload: bytes, digest: str) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        # Resolve after mkdir so an already-present symlink cannot redirect a write.
        try:
            destination.parent.resolve().relative_to(self.root)
        except ValueError as exc:
            raise ArtifactStoreError(f"artifact parent escapes store root: {destination}") from exc

        with self._write_lock:
            if destination.exists():
                self._assert_integrity(destination, digest, len(payload))
                return
            temporary: Path | None = None
            try:
                with tempfile.NamedTemporaryFile(
                    mode="wb",
                    prefix=".tmp-",
                    dir=destination.parent,
                    delete=False,
                ) as handle:
                    temporary = Path(handle.name)
                    handle.write(payload)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, destination)
                temporary = None
                self._fsync_directory(destination.parent)
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)

    @staticmethod
    def _fsync_directory(directory: Path) -> None:
        flags = getattr(os, "O_DIRECTORY", 0) | os.O_RDONLY
        try:
            descriptor = os.open(directory, flags)
        except OSError:
            return
        try:
            os.fsync(descriptor)
        except OSError:
            pass
        finally:
            os.close(descriptor)

    @staticmethod
    def _assert_integrity(path: Path, digest: str, size_bytes: int) -> None:
        if not path.is_file():
            raise ArtifactIntegrityError(f"artifact path is not a file: {path}")
        actual_size = path.stat().st_size
        if actual_size != size_bytes:
            raise ArtifactIntegrityError(
                f"artifact size mismatch at {path}: expected {size_bytes}, got {actual_size}"
            )
        actual_digest = _file_digest(path)
        if actual_digest != digest:
            raise ArtifactIntegrityError(
                f"artifact digest mismatch at {path}: expected {digest}, got {actual_digest}"
            )
