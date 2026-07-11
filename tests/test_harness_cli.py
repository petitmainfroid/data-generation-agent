from __future__ import annotations

import json
from pathlib import Path

from data_generation_agent.cli import main
from data_generation_agent.harness.db import initialize_database
from data_generation_agent.harness.service import HarnessService


ROOT = Path(__file__).resolve().parents[1]


def _json_stdout(capsys) -> dict[str, object]:
    return json.loads(capsys.readouterr().out)


def test_preflight_is_read_only_and_reports_disabled_pipeline(tmp_path: Path, capsys) -> None:
    database = tmp_path / "must-not-exist.sqlite3"
    artifact_root = tmp_path / "must-not-exist-artifacts"
    exit_code = main(
        [
            "preflight",
            "--project-root",
            str(ROOT),
            "--db",
            str(database.relative_to(ROOT) if database.is_relative_to(ROOT) else "var/unused.sqlite3"),
            "--artifact-root",
            "var/unused-artifacts",
        ]
    )
    result = _json_stdout(capsys)
    assert exit_code == 0
    assert result["status"] == "BLOCKED_NOT_IMPLEMENTED"
    assert result["release_ready"] is False
    assert not (ROOT / "var" / "unused.sqlite3").exists()
    assert not (ROOT / "var" / "unused-artifacts").exists()


def test_run_fails_before_creating_database_when_pipeline_is_disabled(tmp_path: Path, capsys) -> None:
    # Paths outside the project are rejected, so use a unique approved relative path.
    relative_db = Path("var") / f"disabled-{tmp_path.name}.sqlite3"
    relative_artifacts = Path("var") / f"disabled-{tmp_path.name}-artifacts"
    database = ROOT / relative_db
    exit_code = main(
        [
            "run",
            "--project-root",
            str(ROOT),
            "--db",
            str(relative_db),
            "--artifact-root",
            str(relative_artifacts),
            "--job-id",
            "blocked-job",
        ]
    )
    result = _json_stdout(capsys)
    assert exit_code == 2
    assert result["status"] == "BLOCKED_NOT_IMPLEMENTED"
    assert not database.exists()
    assert not (ROOT / relative_artifacts).exists()


def test_job_service_is_idempotent_and_lifecycle_is_event_backed(tmp_path: Path) -> None:
    connection = initialize_database(tmp_path / "harness.sqlite3")
    try:
        service = HarnessService(connection, tmp_path / "artifacts")
        first, created = service.create_job("job-1", job_type="AUDIT", config={"a": 1})
        second, created_again = service.create_job("job-1", job_type="AUDIT", config={"a": 1})
        assert first == second
        assert created is True
        assert created_again is False
        running = service.start_job("job-1")
        assert running.status == "RUNNING"
        assert service.start_job("job-1").state_version == running.state_version
        failed = service.fail_job("job-1", retryable=True, error_kind="TIMEOUT")
        assert failed.status == "FAILED"
        retried = service.retry_job("job-1")
        assert retried.status == "RUNNING"
        cancelled = service.cancel_job("job-1")
        assert cancelled.status == "CANCELLED"
        events = service.store.list_events("job", "job-1")
        assert [event.event_type for event in events] == [
            "JOB_CREATED",
            "JOB_STARTED",
            "JOB_FAILED",
            "JOB_RETRIED",
            "JOB_CANCELLED",
        ]
    finally:
        connection.close()


def test_retry_fails_closed_for_non_retryable_failure(tmp_path: Path) -> None:
    connection = initialize_database(tmp_path / "harness.sqlite3")
    try:
        service = HarnessService(connection, tmp_path / "artifacts")
        service.create_job("job-1", job_type="AUDIT")
        service.start_job("job-1")
        service.fail_job("job-1", retryable=False, error_kind="BUSINESS_REJECT")
        try:
            service.retry_job("job-1")
        except Exception as exc:
            assert "not retryable" in str(exc)
        else:
            raise AssertionError("non-retryable Job was retried")
    finally:
        connection.close()


def test_status_missing_database_is_read_only(capsys) -> None:
    relative = Path("var/status-missing-test.sqlite3")
    path = ROOT / relative
    path.unlink(missing_ok=True)
    exit_code = main(
        ["status", "--project-root", str(ROOT), "--db", str(relative)]
    )
    result = _json_stdout(capsys)
    assert exit_code == 0
    assert result["database_exists"] is False
    assert not path.exists()


def test_cli_rejects_path_escape_without_side_effect(capsys, tmp_path: Path) -> None:
    exit_code = main(
        [
            "status",
            "--project-root",
            str(ROOT),
            "--db",
            str(tmp_path / "outside.sqlite3"),
        ]
    )
    result = _json_stdout(capsys)
    assert exit_code == 3
    assert result["ok"] is False
    assert "escapes approved root" in result["error"]
    assert not (tmp_path / "outside.sqlite3").exists()
