from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path
from typing import Any, Sequence

import yaml

from data_generation_agent.harness.db import initialize_database
from data_generation_agent.harness.registry import (
    PipelineDisabledError,
    ToolRegistry,
    ToolRegistryError,
    resolve_within,
)
from data_generation_agent.harness.service import HarnessService, HarnessServiceError
from data_generation_agent.feishu.models import FeishuIngestionError
from data_generation_agent.feishu.profile import BaseProfileError
from data_generation_agent.feishu.runtime import ingest_registered_source
from data_generation_agent.tools.common import redact_secrets


EXIT_OK = 0
EXIT_BLOCKED = 2
EXIT_NOT_FOUND_OR_CONFLICT = 3
EXIT_INTERNAL = 4


def _emit(value: dict[str, Any]) -> None:
    print(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2))


def _common_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--project-root", type=Path, default=Path.cwd())
    parser.add_argument("--config", type=Path, default=Path("configs/question_pipeline.yaml"))
    parser.add_argument("--db", type=Path, default=Path("var/harness.sqlite3"))
    parser.add_argument("--artifact-root", type=Path, default=Path("var/artifacts"))
    return parser


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="data-agent")
    commands = parser.add_subparsers(dest="command", required=True)
    common = _common_parser()

    commands.add_parser("preflight", parents=[common])

    ingest = commands.add_parser("ingest", parents=[common])
    ingest.add_argument("--profile", type=Path, required=True)
    ingest.add_argument("--table-alias", default="seeds")
    ingest.add_argument("--view-alias")
    ingest.add_argument("--source-id")
    ingest.add_argument("--page-size", type=int, default=100)

    run = commands.add_parser("run", parents=[common])
    run.add_argument("--job-id", required=True)
    run.add_argument("--job-type", default="AUDIT")

    status = commands.add_parser("status", parents=[common])
    status.add_argument("--job-id")

    for name in ("resume", "retry", "cancel"):
        command = commands.add_parser(name, parents=[common])
        command.add_argument("--job-id", required=True)

    commands.add_parser("reconcile", parents=[common])
    return parser


def _paths(args: argparse.Namespace, *, require_database: bool = False) -> tuple[Path, Path, Path, Path]:
    root = args.project_root.resolve()
    config = resolve_within(root, args.config, must_exist=True)
    database = resolve_within(root, args.db, must_exist=require_database)
    artifacts = resolve_within(root, args.artifact_root, must_exist=False)
    return root, config, database, artifacts


def preflight_report(root: Path, config_path: Path) -> dict[str, Any]:
    registry = ToolRegistry.load(root)
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    if not isinstance(config, dict):
        raise ToolRegistryError("pipeline config root must be an object")
    stages = config.get("stages")
    if not isinstance(stages, list) or not stages:
        raise ToolRegistryError("pipeline stages are missing")

    diagnostics: list[dict[str, Any]] = []
    release_ready = config.get("full_pipeline_enabled") is True
    for stage in stages:
        if not isinstance(stage, dict):
            raise ToolRegistryError("pipeline stage must be an object")
        stage_id = stage.get("stage_id")
        qualified = stage.get("tool")
        enabled = stage.get("enabled") is True
        diagnostic: dict[str, Any] = {
            "stage_id": stage_id,
            "enabled": enabled,
            "tool": qualified,
            "ready": False,
        }
        if not isinstance(qualified, str) or "@" not in qualified:
            diagnostic["reason"] = "INVALID_TOOL_REFERENCE"
            release_ready = False
        else:
            tool_id, version = qualified.rsplit("@", 1)
            manifest = registry.get(tool_id)
            if manifest.version != version:
                diagnostic["reason"] = "VERSION_MISMATCH"
                release_ready = False
            elif not enabled or not manifest.enabled or manifest.placeholder or manifest.deprecated:
                diagnostic["reason"] = "DISABLED_OR_NOT_IMPLEMENTED"
                release_ready = False
            else:
                diagnostic["ready"] = True
        diagnostics.append(diagnostic)
    return {
        "ok": True,
        "status": "READY" if release_ready else "BLOCKED_NOT_IMPLEMENTED",
        "release_ready": release_ready,
        "full_pipeline_enabled": config.get("full_pipeline_enabled") is True,
        "schema_version": config.get("schema_version"),
        "stages": diagnostics,
        "tool_count": len(registry.list()),
    }


def _status_read_only(database: Path, job_id: str | None) -> dict[str, Any]:
    if not database.is_file():
        return {"ok": True, "database_exists": False, "jobs": [], "counts": {}}
    uri = database.resolve().as_uri() + "?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    connection.row_factory = sqlite3.Row
    try:
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        if "jobs" not in tables:
            return {"ok": True, "database_exists": True, "initialized": False}
        if job_id is not None:
            row = connection.execute(
                "SELECT * FROM jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
            if row is None:
                raise HarnessServiceError(f"Job does not exist: {job_id}")
            return {
                "ok": True,
                "database_exists": True,
                "job": {key: row[key] for key in row.keys()},
            }
        counts = {
            str(row["status"]): int(row["count"])
            for row in connection.execute(
                "SELECT status, COUNT(*) AS count FROM jobs GROUP BY status"
            ).fetchall()
        }
        return {"ok": True, "database_exists": True, "counts": counts}
    finally:
        connection.close()


def run_command(args: argparse.Namespace) -> int:
    root, config, database, artifacts = _paths(
        args, require_database=args.command in {"resume", "retry", "cancel", "reconcile"}
    )
    if args.command == "preflight":
        _emit(preflight_report(root, config))
        return EXIT_OK
    if args.command == "status":
        _emit(_status_read_only(database, args.job_id))
        return EXIT_OK
    if args.command == "ingest":
        profile = resolve_within(root, args.profile, must_exist=True)
        _emit(
            ingest_registered_source(
                profile_path=profile,
                database_path=database,
                artifact_root=artifacts,
                table_alias=args.table_alias,
                source_id=args.source_id,
                view_alias=args.view_alias,
                page_size=args.page_size,
            )
        )
        return EXIT_OK

    if args.command == "run":
        registry = ToolRegistry.load(root)
        registry.assert_pipeline_runnable(config.relative_to(root))

    connection = initialize_database(database)
    try:
        service = HarnessService(connection, artifacts)
        if args.command == "run":
            pipeline_config = yaml.safe_load(config.read_text(encoding="utf-8"))
            job, created = service.create_job(
                args.job_id,
                job_type=args.job_type,
                config={
                    "pipeline_id": pipeline_config.get("pipeline_id"),
                    "schema_version": pipeline_config.get("schema_version"),
                },
            )
            job = service.start_job(job.job_id)
            _emit({"ok": True, "created": created, "job": job.as_dict()})
        elif args.command == "resume":
            _emit({"ok": True, "job": service.resume_job(args.job_id).as_dict()})
        elif args.command == "retry":
            _emit({"ok": True, "job": service.retry_job(args.job_id).as_dict()})
        elif args.command == "cancel":
            _emit({"ok": True, "job": service.cancel_job(args.job_id).as_dict()})
        elif args.command == "reconcile":
            _emit({"ok": True, "report": service.reconcile()})
        else:
            raise HarnessServiceError(f"unsupported command: {args.command}")
    finally:
        connection.close()
    return EXIT_OK


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return run_command(args)
    except PipelineDisabledError as exc:
        _emit(
            {
                "ok": False,
                "status": "BLOCKED_NOT_IMPLEMENTED",
                "error": redact_secrets(str(exc)),
            }
        )
        return EXIT_BLOCKED
    except (
        ToolRegistryError,
        HarnessServiceError,
        FeishuIngestionError,
        BaseProfileError,
        ValueError,
        sqlite3.IntegrityError,
    ) as exc:
        _emit({"ok": False, "status": "REJECTED", "error": redact_secrets(str(exc))})
        return EXIT_NOT_FOUND_OR_CONFLICT
    except sqlite3.DatabaseError as exc:
        _emit({"ok": False, "status": "ERROR", "error": redact_secrets(str(exc))})
        return EXIT_INTERNAL


if __name__ == "__main__":
    raise SystemExit(main())
