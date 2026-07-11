from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from .db import transaction
from .state import (
    DEFAULT_STATE_MACHINE,
    StateConflictError,
    StateMachine,
    StateVersionConflictError,
    TransitionResult,
)


Clock = Callable[[], datetime | float | int]


class StoreError(RuntimeError):
    """Base class for durable store errors."""


class AggregateNotFoundError(StoreError):
    """A durable aggregate row does not exist."""


@dataclass(frozen=True)
class EventRecord:
    event_id: int
    event_key: str
    job_id: str | None
    aggregate_type: str
    aggregate_id: str
    event_type: str
    payload: dict[str, Any]
    causation_id: str | None
    correlation_id: str | None
    created_at: str


def system_clock() -> datetime:
    return datetime.now(timezone.utc)


def clock_timestamp(clock: Clock) -> tuple[float, str]:
    value = clock()
    if isinstance(value, datetime):
        current = value
        if current.tzinfo is None:
            current = current.replace(tzinfo=timezone.utc)
        current = current.astimezone(timezone.utc)
        epoch = current.timestamp()
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        epoch = float(value)
        current = datetime.fromtimestamp(epoch, tz=timezone.utc)
    else:
        raise TypeError("clock must return datetime or epoch seconds")
    return epoch, current.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _expected_states(value: str | Iterable[str]) -> frozenset[str]:
    if isinstance(value, str):
        states = frozenset({value})
    else:
        states = frozenset(value)
    if not states or not all(isinstance(item, str) and item for item in states):
        raise ValueError("expected_status must contain at least one non-empty state")
    return states


def _payload_json(payload: Mapping[str, Any] | None) -> str:
    return json.dumps(dict(payload or {}), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class HarnessStore:
    """Transactional state and append-only event access.

    ``transition`` is the only state mutation API exposed by this class.  The
    compare-and-set update and its event append share one ``BEGIN IMMEDIATE``
    transaction, so either both become visible or neither does.
    """

    def __init__(
        self,
        connection: sqlite3.Connection,
        *,
        state_machine: StateMachine = DEFAULT_STATE_MACHINE,
        clock: Clock = system_clock,
    ) -> None:
        self.connection = connection
        self.state_machine = state_machine
        self.clock = clock

    def get_status(self, aggregate_type: str, aggregate_id: str) -> str:
        spec = self.state_machine.spec_for(aggregate_type)
        row = self.connection.execute(
            f"SELECT {spec.state_column} AS status FROM {spec.table} "
            f"WHERE {spec.id_column} = ?",
            (aggregate_id,),
        ).fetchone()
        if row is None:
            raise AggregateNotFoundError(f"aggregate not found: {aggregate_type}/{aggregate_id}")
        return str(row["status"])

    def transition(
        self,
        aggregate_type: str,
        aggregate_id: str,
        expected_status: str | Iterable[str],
        new_status: str,
        *,
        event_type: str,
        expected_version: int | None = None,
        payload: Mapping[str, Any] | None = None,
        event_key: str | None = None,
        job_id: str | None = None,
        causation_id: str | None = None,
        correlation_id: str | None = None,
    ) -> TransitionResult:
        if not event_type:
            raise ValueError("event_type must not be empty")
        expected = _expected_states(expected_status)
        spec = self.state_machine.spec_for(aggregate_type)
        serialized = dict(payload or {})
        event_key = event_key or f"evt_{uuid.uuid4().hex}"
        _, created_at = clock_timestamp(self.clock)

        with transaction(self.connection, mode="IMMEDIATE"):
            version_select = (
                f", {spec.version_column} AS state_version" if spec.version_column else ""
            )
            row = self.connection.execute(
                f"SELECT {spec.state_column} AS status{version_select} FROM {spec.table} "
                f"WHERE {spec.id_column} = ?",
                (aggregate_id,),
            ).fetchone()
            actual = None if row is None else str(row["status"])
            if actual not in expected:
                raise StateConflictError(aggregate_type, aggregate_id, expected, actual)
            self.state_machine.validate(aggregate_type, actual, new_status)
            actual_version = 0 if spec.version_column is None else int(row["state_version"])
            if expected_version is not None and expected_version != actual_version:
                raise StateVersionConflictError(
                    aggregate_type,
                    aggregate_id,
                    expected_version,
                    actual_version,
                )

            assignments = [f"{spec.state_column} = ?"]
            parameters: list[Any] = [new_status]
            if spec.version_column:
                assignments.append(f"{spec.version_column} = {spec.version_column} + 1")
            if spec.updated_at_column:
                assignments.append(f"{spec.updated_at_column} = ?")
                parameters.append(created_at)
            where = [f"{spec.id_column} = ?", f"{spec.state_column} = ?"]
            parameters.extend([aggregate_id, actual])
            if spec.version_column:
                where.append(f"{spec.version_column} = ?")
                parameters.append(actual_version)
            changed = self.connection.execute(
                f"UPDATE {spec.table} SET {', '.join(assignments)} "
                f"WHERE {' AND '.join(where)}",
                parameters,
            )
            if changed.rowcount != 1:
                current = self.connection.execute(
                    f"SELECT {spec.state_column} AS status FROM {spec.table} "
                    f"WHERE {spec.id_column} = ?",
                    (aggregate_id,),
                ).fetchone()
                current_status = None if current is None else str(current["status"])
                raise StateConflictError(
                    aggregate_type,
                    aggregate_id,
                    expected,
                    current_status,
                )

            if job_id is None:
                job_id = self._job_id_for(aggregate_type, aggregate_id)
            serialized.update(
                {
                    "previous_status": actual,
                    "new_status": new_status,
                    "previous_version": actual_version,
                    "new_version": actual_version + (1 if spec.version_column else 0),
                }
            )
            event_id = self._append_event(
                event_key=event_key,
                job_id=job_id,
                aggregate_type=aggregate_type,
                aggregate_id=aggregate_id,
                event_type=event_type,
                payload_json=_payload_json(serialized),
                causation_id=causation_id,
                correlation_id=correlation_id,
                created_at=created_at,
            )

        return TransitionResult(
            aggregate_type=aggregate_type,
            aggregate_id=aggregate_id,
            previous_status=actual,
            new_status=new_status,
            previous_version=actual_version,
            new_version=actual_version + (1 if spec.version_column else 0),
            event_id=event_id,
        )

    def append_event(
        self,
        aggregate_type: str,
        aggregate_id: str,
        event_type: str,
        *,
        payload: Mapping[str, Any] | None = None,
        event_key: str | None = None,
        job_id: str | None = None,
        causation_id: str | None = None,
        correlation_id: str | None = None,
    ) -> int:
        if not aggregate_type or not aggregate_id or not event_type:
            raise ValueError("aggregate_type, aggregate_id, and event_type are required")
        event_key = event_key or f"evt_{uuid.uuid4().hex}"
        _, created_at = clock_timestamp(self.clock)
        with transaction(self.connection, mode="IMMEDIATE"):
            return self._append_event(
                event_key=event_key,
                job_id=job_id,
                aggregate_type=aggregate_type,
                aggregate_id=aggregate_id,
                event_type=event_type,
                payload_json=_payload_json(payload),
                causation_id=causation_id,
                correlation_id=correlation_id,
                created_at=created_at,
            )

    def list_events(
        self,
        aggregate_type: str,
        aggregate_id: str,
        *,
        after_event_id: int = 0,
    ) -> list[EventRecord]:
        rows = self.connection.execute(
            "SELECT event_id, event_key, job_id, aggregate_type, aggregate_id, "
            "event_type, payload_json, causation_id, correlation_id, created_at "
            "FROM events WHERE aggregate_type = ? AND aggregate_id = ? "
            "AND event_id > ? ORDER BY event_id",
            (aggregate_type, aggregate_id, after_event_id),
        ).fetchall()
        return [
            EventRecord(
                event_id=int(row["event_id"]),
                event_key=str(row["event_key"]),
                job_id=None if row["job_id"] is None else str(row["job_id"]),
                aggregate_type=str(row["aggregate_type"]),
                aggregate_id=str(row["aggregate_id"]),
                event_type=str(row["event_type"]),
                payload=json.loads(row["payload_json"]),
                causation_id=None if row["causation_id"] is None else str(row["causation_id"]),
                correlation_id=(
                    None if row["correlation_id"] is None else str(row["correlation_id"])
                ),
                created_at=str(row["created_at"]),
            )
            for row in rows
        ]

    def _append_event(
        self,
        *,
        event_key: str,
        job_id: str | None,
        aggregate_type: str,
        aggregate_id: str,
        event_type: str,
        payload_json: str,
        causation_id: str | None,
        correlation_id: str | None,
        created_at: str,
    ) -> int:
        cursor = self.connection.execute(
            "INSERT INTO events "
            "(event_key, job_id, aggregate_type, aggregate_id, event_type, "
            "payload_json, causation_id, correlation_id, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                event_key,
                job_id,
                aggregate_type,
                aggregate_id,
                event_type,
                payload_json,
                causation_id,
                correlation_id,
                created_at,
            ),
        )
        return int(cursor.lastrowid)

    def _job_id_for(self, aggregate_type: str, aggregate_id: str) -> str | None:
        if aggregate_type == "job":
            return aggregate_id
        if aggregate_type == "candidate":
            row = self.connection.execute(
                "SELECT job_id FROM candidates WHERE candidate_id = ?",
                (aggregate_id,),
            ).fetchone()
            return None if row is None else str(row["job_id"])
        if aggregate_type == "attempt":
            row = self.connection.execute(
                "SELECT c.job_id FROM attempts AS a "
                "JOIN revisions AS r ON r.revision_id = a.revision_id "
                "JOIN candidates AS c ON c.candidate_id = r.candidate_id "
                "WHERE a.attempt_id = ?",
                (aggregate_id,),
            ).fetchone()
            return None if row is None else str(row["job_id"])
        return None
