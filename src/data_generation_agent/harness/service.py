from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from .artifacts import ArtifactStore
from .db import transaction
from .store import HarnessStore


class HarnessServiceError(RuntimeError):
    """A durable Harness lifecycle operation was rejected."""


class JobNotFoundError(HarnessServiceError):
    """The requested Job does not exist."""


class JobConflictError(HarnessServiceError):
    """A stable Job ID was reused with different immutable inputs."""


@dataclass(frozen=True)
class JobRecord:
    job_id: str
    job_type: str
    status: str
    policy_digest: str
    config: dict[str, Any]
    state_version: int
    cancel_requested: bool
    created_at: str
    updated_at: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


def canonical_json(value: Mapping[str, Any]) -> str:
    try:
        return json.dumps(
            dict(value), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
    except (TypeError, ValueError) as exc:
        raise HarnessServiceError("Job config must be JSON serializable") from exc


def digest_policy(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


class HarnessService:
    """Small transactional service for Job lifecycle and local reconciliation.

    The model-facing pipeline never calls this object directly. The orchestrator
    uses it to create a stable Job, append lifecycle events, and resume only
    non-terminal work. External side effects remain frozen Outbox rows.
    """

    def __init__(self, connection: sqlite3.Connection, artifact_root: Path) -> None:
        self.connection = connection
        self.store = HarnessStore(connection)
        self.artifacts = ArtifactStore(artifact_root, connection)

    def create_job(
        self,
        job_id: str,
        *,
        job_type: str,
        config: Mapping[str, Any] | None = None,
        policy_digest: str | None = None,
    ) -> tuple[JobRecord, bool]:
        if not isinstance(job_id, str) or not job_id.strip():
            raise HarnessServiceError("job_id must not be empty")
        if not isinstance(job_type, str) or not job_type.strip():
            raise HarnessServiceError("job_type must not be empty")
        config_value = dict(config or {})
        config_json = canonical_json(config_value)
        expected_digest = policy_digest or hashlib.sha256(
            config_json.encode("utf-8")
        ).hexdigest()
        if len(expected_digest) != 64 or any(
            char not in "0123456789abcdef" for char in expected_digest
        ):
            raise HarnessServiceError("policy_digest must be a lowercase SHA-256")
        timestamp = utc_now()

        with transaction(self.connection, mode="IMMEDIATE"):
            inserted = self.connection.execute(
                """
                INSERT INTO jobs(
                    job_id, job_type, status, policy_digest, config_json,
                    state_version, cancel_requested, created_at, updated_at
                ) VALUES (?, ?, 'PENDING', ?, ?, 0, 0, ?, ?)
                ON CONFLICT(job_id) DO NOTHING
                """,
                (job_id, job_type, expected_digest, config_json, timestamp, timestamp),
            ).rowcount
            row = self.connection.execute(
                "SELECT * FROM jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
            if row is None:
                raise HarnessServiceError(f"Job disappeared after creation: {job_id}")
            if (
                row["job_type"] != job_type
                or row["policy_digest"] != expected_digest
                or row["config_json"] != config_json
            ):
                raise JobConflictError(
                    f"job_id already belongs to different immutable inputs: {job_id}"
                )
            if inserted:
                self.store.append_event(
                    "job",
                    job_id,
                    "JOB_CREATED",
                    job_id=job_id,
                    event_key=f"job:{job_id}:created",
                    payload={
                        "job_type": job_type,
                        "policy_digest": expected_digest,
                    },
                )
        return self._record(row), bool(inserted)

    def get_job(self, job_id: str) -> JobRecord:
        row = self.connection.execute(
            "SELECT * FROM jobs WHERE job_id = ?", (job_id,)
        ).fetchone()
        if row is None:
            raise JobNotFoundError(f"Job does not exist: {job_id}")
        return self._record(row)

    def list_jobs(self, *, limit: int = 100) -> list[JobRecord]:
        if limit <= 0:
            return []
        rows = self.connection.execute(
            "SELECT * FROM jobs ORDER BY created_at DESC, job_id LIMIT ?", (limit,)
        ).fetchall()
        return [self._record(row) for row in rows]

    def start_job(self, job_id: str) -> JobRecord:
        current = self.get_job(job_id)
        if current.status == "RUNNING":
            return current
        if current.status != "PENDING":
            raise HarnessServiceError(
                f"only a PENDING Job can start: {job_id} ({current.status})"
            )
        self.store.transition(
            "job",
            job_id,
            "PENDING",
            "RUNNING",
            event_type="JOB_STARTED",
            event_key=f"job:{job_id}:started:v{current.state_version + 1}",
            job_id=job_id,
        )
        return self.get_job(job_id)

    def resume_job(self, job_id: str) -> JobRecord:
        current = self.get_job(job_id)
        if current.status == "RUNNING":
            return current
        resumable = {"PENDING", "FAILED", "COMPLETED_PARTIAL"}
        if current.status not in resumable:
            raise HarnessServiceError(
                f"terminal or blocked Job cannot resume: {job_id} ({current.status})"
            )
        self.store.transition(
            "job",
            job_id,
            current.status,
            "RUNNING",
            event_type="JOB_RESUMED",
            event_key=f"job:{job_id}:resumed:v{current.state_version + 1}",
            job_id=job_id,
        )
        return self.get_job(job_id)

    def fail_job(
        self, job_id: str, *, retryable: bool, error_kind: str = "INTERNAL_ERROR"
    ) -> JobRecord:
        current = self.get_job(job_id)
        if current.status != "RUNNING":
            raise HarnessServiceError(
                f"only a RUNNING Job can fail: {job_id} ({current.status})"
            )
        self.store.transition(
            "job",
            job_id,
            "RUNNING",
            "FAILED",
            event_type="JOB_FAILED",
            event_key=f"job:{job_id}:failed:v{current.state_version + 1}",
            job_id=job_id,
            payload={"retryable": bool(retryable), "error_kind": error_kind},
        )
        return self.get_job(job_id)

    def retry_job(self, job_id: str) -> JobRecord:
        current = self.get_job(job_id)
        if current.status != "FAILED":
            raise HarnessServiceError(
                f"only a retryable FAILED Job can retry: {job_id} ({current.status})"
            )
        events = self.store.list_events("job", job_id)
        failure = next(
            (event for event in reversed(events) if event.event_type == "JOB_FAILED"),
            None,
        )
        if failure is None or failure.payload.get("retryable") is not True:
            raise HarnessServiceError(f"Job failure is not retryable: {job_id}")
        self.store.transition(
            "job",
            job_id,
            "FAILED",
            "RUNNING",
            event_type="JOB_RETRIED",
            event_key=f"job:{job_id}:retried:v{current.state_version + 1}",
            job_id=job_id,
        )
        return self.get_job(job_id)

    def cancel_job(self, job_id: str) -> JobRecord:
        current = self.get_job(job_id)
        if current.status == "CANCELLED":
            return current
        if current.status not in {"PENDING", "RUNNING", "FAILED"}:
            raise HarnessServiceError(
                f"terminal Job cannot be cancelled: {job_id} ({current.status})"
            )
        self.store.transition(
            "job",
            job_id,
            current.status,
            "CANCELLED",
            event_type="JOB_CANCELLED",
            event_key=f"job:{job_id}:cancelled:v{current.state_version + 1}",
            job_id=job_id,
        )
        return self.get_job(job_id)

    def status_snapshot(self, job_id: str | None = None) -> dict[str, Any]:
        if job_id is not None:
            job = self.get_job(job_id)
            events = self.store.list_events("job", job_id)
            return {
                "job": job.as_dict(),
                "event_count": len(events),
                "last_event": events[-1].event_type if events else None,
            }
        jobs = self.list_jobs()
        counts = {
            str(row["status"]): int(row["count"])
            for row in self.connection.execute(
                "SELECT status, COUNT(*) AS count FROM jobs GROUP BY status"
            ).fetchall()
        }
        return {"jobs": [job.as_dict() for job in jobs], "counts": counts}

    def reconcile(self) -> dict[str, Any]:
        artifact_report = self.artifacts.reconcile()
        outbox_counts = {
            str(row["status"]): int(row["count"])
            for row in self.connection.execute(
                "SELECT status, COUNT(*) AS count FROM outbox GROUP BY status"
            ).fetchall()
        }
        return {
            "artifacts": artifact_report.as_dict(),
            "outbox_counts": outbox_counts,
            "ok": artifact_report.ok,
        }

    @staticmethod
    def _record(row: sqlite3.Row) -> JobRecord:
        config = json.loads(row["config_json"])
        if not isinstance(config, dict):
            raise HarnessServiceError(f"Job config is not an object: {row['job_id']}")
        return JobRecord(
            job_id=str(row["job_id"]),
            job_type=str(row["job_type"]),
            status=str(row["status"]),
            policy_digest=str(row["policy_digest"]),
            config=config,
            state_version=int(row["state_version"]),
            cancel_requested=bool(row["cancel_requested"]),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )
