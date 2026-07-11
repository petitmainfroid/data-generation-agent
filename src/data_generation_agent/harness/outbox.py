from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

from data_generation_agent.tools.common import redact_secrets

from .db import transaction


class OutboxError(RuntimeError):
    """The transactional Outbox contract could not be satisfied."""


class OutboxConflictError(OutboxError):
    """A dedupe key was reused for a different immutable write."""


class OutboxLeaseError(OutboxError):
    """A worker attempted to mutate a message without its active lease."""


class SecretPayloadError(OutboxError):
    """A secret-like value was about to be persisted in an Outbox payload."""


@dataclass(frozen=True)
class OutboxRecord:
    outbox_id: str
    job_id: str
    aggregate_type: str
    aggregate_id: str
    destination: str
    operation: str
    dedupe_key: str
    payload: dict[str, Any]
    payload_hash: str
    payload_artifact_id: str | None
    status: str
    attempt_count: int
    available_at: str
    lease_owner: str | None
    lease_token: str | None
    lease_expires_at: str | None
    last_error: str | None
    response: Any
    created_at: str
    updated_at: str
    sent_at: str | None

    @property
    def delivery_key(self) -> str:
        """Key a remote idempotent API must receive on every retry."""

        return self.dedupe_key


_SECRET_KEYS = {
    "api_key",
    "apikey",
    "authorization",
    "cookie",
    "credential",
    "credentials",
    "password",
    "private_key",
    "refresh_token",
    "secret",
    "token",
    "access_token",
    "app_secret",
    "client_secret",
}
_SECRET_SUFFIXES = ("_api_key", "_password", "_private_key", "_secret", "_token")
_SECRET_VALUE_PATTERNS = (
    re.compile(r"(?i)\bBearer\s+[^\s,;]+"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{12,}\b"),
    re.compile(r"(?i)\b(?:api[-_]?key|access[-_]?token|refresh[-_]?token|password|secret)\s*[:=]"),
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: datetime | str | None = None) -> datetime:
    if value is None:
        return _now()
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise OutboxError(f"invalid timestamp: {value}") from exc
    else:
        raise TypeError("timestamp must be datetime, ISO string, or None")
    if parsed.tzinfo is None:
        raise OutboxError("timestamp must include a timezone")
    return parsed.astimezone(timezone.utc)


def _timestamp(value: datetime | str | None = None) -> str:
    return _as_utc(value).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _canonical_json(value: Any) -> tuple[str, str]:
    try:
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise OutboxError("Outbox value must be JSON serializable") from exc
    return encoded, hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _assert_no_secret(value: Any, path: str = "payload") -> None:
    if isinstance(value, Mapping):
        for raw_key, child in value.items():
            key = str(raw_key)
            normalized = key.strip().lower().replace("-", "_")
            if normalized in _SECRET_KEYS or normalized.endswith(_SECRET_SUFFIXES):
                raise SecretPayloadError(f"secret-like key is forbidden at {path}.{key}")
            _assert_no_secret(child, f"{path}.{key}")
        return
    if isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            _assert_no_secret(child, f"{path}[{index}]")
        return
    if isinstance(value, str):
        for pattern in _SECRET_VALUE_PATTERNS:
            if pattern.search(value):
                raise SecretPayloadError(f"secret-like value is forbidden at {path}")


class OutboxStore:
    """Transactional, leased delivery queue for already-computed side effects.

    The queue stores a frozen payload, never a model callback. A lost remote ACK
    therefore reclaims the same Outbox row and delivery key; it cannot rerun the
    model or manufacture a second logical write.
    """

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def enqueue(
        self,
        *,
        job_id: str,
        aggregate_type: str,
        aggregate_id: str,
        destination: str,
        operation: str,
        dedupe_key: str,
        payload: Mapping[str, Any],
        payload_artifact_id: str | None = None,
        available_at: datetime | str | None = None,
    ) -> OutboxRecord:
        for name, value in (
            ("job_id", job_id),
            ("aggregate_type", aggregate_type),
            ("aggregate_id", aggregate_id),
            ("destination", destination),
            ("dedupe_key", dedupe_key),
        ):
            if not isinstance(value, str) or not value.strip():
                raise OutboxError(f"{name} must not be empty")
        operation_value = operation.upper()
        if operation_value not in {"CREATE", "UPSERT", "PATCH"}:
            raise OutboxError(f"unsupported Outbox operation: {operation}")
        if not isinstance(payload, Mapping):
            raise OutboxError("Outbox payload must be a JSON object")
        payload_value = dict(payload)
        _assert_no_secret(payload_value)
        payload_json, payload_hash = _canonical_json(payload_value)
        outbox_id = "outbox:" + hashlib.sha256(dedupe_key.encode("utf-8")).hexdigest()
        now = _timestamp()
        available = _timestamp(available_at)

        with transaction(self.connection):
            self.connection.execute(
                """
                INSERT INTO outbox(
                    outbox_id, job_id, aggregate_type, aggregate_id, destination,
                    operation, dedupe_key, payload_json, payload_hash,
                    payload_artifact_id, status, attempt_count, available_at,
                    lease_owner, lease_token, lease_expires_at, last_error,
                    response_json, created_at, updated_at, sent_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'PENDING', 0, ?,
                          NULL, NULL, NULL, NULL, NULL, ?, ?, NULL)
                ON CONFLICT(dedupe_key) DO NOTHING
                """,
                (
                    outbox_id,
                    job_id,
                    aggregate_type,
                    aggregate_id,
                    destination,
                    operation_value,
                    dedupe_key,
                    payload_json,
                    payload_hash,
                    payload_artifact_id,
                    available,
                    now,
                    now,
                ),
            )
            row = self.connection.execute(
                "SELECT * FROM outbox WHERE dedupe_key = ?", (dedupe_key,)
            ).fetchone()
            if row is None:
                raise OutboxError(f"Outbox row missing after enqueue: {dedupe_key}")
            immutable = {
                "outbox_id": outbox_id,
                "job_id": job_id,
                "aggregate_type": aggregate_type,
                "aggregate_id": aggregate_id,
                "destination": destination,
                "operation": operation_value,
                "payload_hash": payload_hash,
                "payload_artifact_id": payload_artifact_id,
            }
            conflicts = [key for key, expected in immutable.items() if row[key] != expected]
            if conflicts:
                raise OutboxConflictError(
                    f"dedupe key already identifies a different write ({', '.join(conflicts)}): {dedupe_key}"
                )
        return self._record(row)

    def claim(
        self,
        worker_id: str,
        *,
        limit: int = 1,
        lease_seconds: float = 60,
        now: datetime | str | None = None,
    ) -> list[OutboxRecord]:
        if not worker_id.strip():
            raise OutboxError("worker_id must not be empty")
        if limit <= 0:
            return []
        if lease_seconds <= 0:
            raise OutboxError("lease_seconds must be positive")
        claimed_at = _as_utc(now)
        claimed_timestamp = _timestamp(claimed_at)
        expires_at = _timestamp(claimed_at + timedelta(seconds=lease_seconds))
        claimed_ids: list[str] = []

        with transaction(self.connection, mode="IMMEDIATE"):
            rows = self.connection.execute(
                """
                SELECT outbox_id
                FROM outbox
                WHERE (
                    status IN ('PENDING', 'RETRY_WAIT') AND available_at <= ?
                ) OR (
                    status = 'IN_FLIGHT' AND lease_expires_at IS NOT NULL
                    AND lease_expires_at <= ?
                )
                ORDER BY available_at, created_at, outbox_id
                LIMIT ?
                """,
                (claimed_timestamp, claimed_timestamp, limit),
            ).fetchall()
            for row in rows:
                outbox_id = row["outbox_id"]
                lease_token = uuid.uuid4().hex
                self.connection.execute(
                    """
                    UPDATE outbox
                    SET status = 'IN_FLIGHT', attempt_count = attempt_count + 1,
                        lease_owner = ?, lease_token = ?, lease_expires_at = ?,
                        updated_at = ?
                    WHERE outbox_id = ?
                    """,
                    (worker_id, lease_token, expires_at, claimed_timestamp, outbox_id),
                )
                claimed_ids.append(outbox_id)

            if not claimed_ids:
                return []
            placeholders = ",".join("?" for _ in claimed_ids)
            claimed_rows = self.connection.execute(
                f"SELECT * FROM outbox WHERE outbox_id IN ({placeholders})",
                claimed_ids,
            ).fetchall()
        by_id = {row["outbox_id"]: self._record(row) for row in claimed_rows}
        return [by_id[outbox_id] for outbox_id in claimed_ids]

    def ack(
        self,
        outbox_id: str,
        lease_token: str,
        *,
        response: Any = None,
        now: datetime | str | None = None,
    ) -> OutboxRecord:
        if response is not None:
            _assert_no_secret(response, "response")
        response_json = None if response is None else _canonical_json(response)[0]
        timestamp = _timestamp(now)
        with transaction(self.connection, mode="IMMEDIATE"):
            row = self._required_row(outbox_id)
            if row["status"] == "SENT":
                return self._record(row)
            self._assert_lease(row, lease_token)
            self.connection.execute(
                """
                UPDATE outbox
                SET status = 'SENT', response_json = ?, sent_at = ?, updated_at = ?,
                    lease_owner = NULL, lease_token = NULL, lease_expires_at = NULL,
                    last_error = NULL
                WHERE outbox_id = ?
                """,
                (response_json, timestamp, timestamp, outbox_id),
            )
            updated = self._required_row(outbox_id)
        return self._record(updated)

    def retry(
        self,
        outbox_id: str,
        lease_token: str,
        error: str,
        *,
        delay_seconds: float = 0,
        max_attempts: int | None = None,
        now: datetime | str | None = None,
    ) -> OutboxRecord:
        if delay_seconds < 0:
            raise OutboxError("delay_seconds must not be negative")
        if max_attempts is not None and max_attempts <= 0:
            raise OutboxError("max_attempts must be positive")
        retried_at = _as_utc(now)
        timestamp = _timestamp(retried_at)
        available = _timestamp(retried_at + timedelta(seconds=delay_seconds))
        safe_error = redact_secrets(str(error))[:4000]

        with transaction(self.connection, mode="IMMEDIATE"):
            row = self._required_row(outbox_id)
            self._assert_lease(row, lease_token)
            terminal = max_attempts is not None and row["attempt_count"] >= max_attempts
            status = "FAILED" if terminal else "RETRY_WAIT"
            self.connection.execute(
                """
                UPDATE outbox
                SET status = ?, available_at = ?, last_error = ?, updated_at = ?,
                    lease_owner = NULL, lease_token = NULL, lease_expires_at = NULL
                WHERE outbox_id = ?
                """,
                (status, available, safe_error, timestamp, outbox_id),
            )
            updated = self._required_row(outbox_id)
        return self._record(updated)

    def get(self, outbox_id: str) -> OutboxRecord | None:
        row = self.connection.execute(
            "SELECT * FROM outbox WHERE outbox_id = ?", (outbox_id,)
        ).fetchone()
        return self._record(row) if row is not None else None

    def get_by_dedupe_key(self, dedupe_key: str) -> OutboxRecord | None:
        row = self.connection.execute(
            "SELECT * FROM outbox WHERE dedupe_key = ?", (dedupe_key,)
        ).fetchone()
        return self._record(row) if row is not None else None

    def _required_row(self, outbox_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM outbox WHERE outbox_id = ?", (outbox_id,)
        ).fetchone()
        if row is None:
            raise OutboxError(f"Outbox row does not exist: {outbox_id}")
        return row

    @staticmethod
    def _assert_lease(row: sqlite3.Row, lease_token: str) -> None:
        if row["status"] != "IN_FLIGHT":
            raise OutboxLeaseError(
                f"Outbox row is not in flight: {row['outbox_id']} ({row['status']})"
            )
        if not lease_token or row["lease_token"] != lease_token:
            raise OutboxLeaseError(f"Outbox lease token does not match: {row['outbox_id']}")

    @staticmethod
    def _record(row: sqlite3.Row) -> OutboxRecord:
        payload = json.loads(row["payload_json"])
        if not isinstance(payload, dict):
            raise OutboxError(f"Outbox payload is not an object: {row['outbox_id']}")
        response = None if row["response_json"] is None else json.loads(row["response_json"])
        return OutboxRecord(
            outbox_id=row["outbox_id"],
            job_id=row["job_id"],
            aggregate_type=row["aggregate_type"],
            aggregate_id=row["aggregate_id"],
            destination=row["destination"],
            operation=row["operation"],
            dedupe_key=row["dedupe_key"],
            payload=payload,
            payload_hash=row["payload_hash"],
            payload_artifact_id=row["payload_artifact_id"],
            status=row["status"],
            attempt_count=row["attempt_count"],
            available_at=row["available_at"],
            lease_owner=row["lease_owner"],
            lease_token=row["lease_token"],
            lease_expires_at=row["lease_expires_at"],
            last_error=row["last_error"],
            response=response,
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            sent_at=row["sent_at"],
        )
