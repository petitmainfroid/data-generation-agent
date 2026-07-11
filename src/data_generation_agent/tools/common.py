from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


ID_KEYS = ("sft_id", "candidate_key", "generated_id", "question_id", "id")


class ToolContractError(RuntimeError):
    """Raised when tool inputs or legacy outputs violate the public contract."""


class LegacyProcessError(RuntimeError):
    """Raised when a legacy subprocess exits unsuccessfully."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not path.exists():
        return rows
    with path.open("r", encoding="utf-8-sig") as handle:
        for line_number, raw in enumerate(handle, 1):
            line = raw.strip()
            if not line:
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ToolContractError(f"invalid JSONL at {path}:{line_number}: {exc}") from exc
            if not isinstance(value, dict):
                raise ToolContractError(f"JSONL row must be an object at {path}:{line_number}")
            rows.append(value)
    return rows


def atomic_write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    temporary.replace(path)


def atomic_write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def stable_id(row: dict[str, Any], index: int) -> str:
    for key in ID_KEYS:
        value = row.get(key)
        if value not in (None, ""):
            return str(value)
    return f"row_{index}"


def validate_question_rows(path: Path) -> list[tuple[str, dict[str, Any]]]:
    rows = read_jsonl(path)
    validated: list[tuple[str, dict[str, Any]]] = []
    seen: set[str] = set()
    for index, row in enumerate(rows):
        candidate_id = stable_id(row, index)
        if candidate_id in seen:
            raise ToolContractError(f"duplicate candidate id: {candidate_id}")
        seen.add(candidate_id)
        question = row.get("question")
        if not isinstance(question, str) or not question.strip():
            raise ToolContractError(f"missing non-empty question for candidate: {candidate_id}")
        validated.append((candidate_id, row))
    if not validated:
        raise ToolContractError(f"input contains no question rows: {path}")
    return validated


def input_hash(row: dict[str, Any]) -> str:
    canonical = json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def stable_result_id(tool_id: str, *parts: Any) -> str:
    canonical = json.dumps(parts, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return f"{tool_id}:{digest}"


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_env_values(path: Path | None) -> dict[str, str]:
    if path is None:
        return {}
    if not path.exists():
        raise ToolContractError(f"env file does not exist: {path}")
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        key, value = line.split("=", 1)
        key = key.strip()
        if key:
            values[key] = value.strip().strip("'\"")
    return values


def run_legacy_process(
    command: list[str],
    *,
    cwd: Path,
    output_dir: Path,
    env_file: Path | None = None,
    env_overrides: dict[str, str] | None = None,
    timeout_seconds: int = 3600,
) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    environment = os.environ.copy()
    environment.update(load_env_values(env_file))
    environment.update(env_overrides or {})
    started_at = utc_now()
    completed = subprocess.run(
        command,
        cwd=cwd,
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout_seconds,
        check=False,
    )
    (output_dir / "legacy.stdout.log").write_text(completed.stdout, encoding="utf-8")
    (output_dir / "legacy.stderr.log").write_text(completed.stderr, encoding="utf-8")
    atomic_write_json(
        output_dir / "legacy.process.json",
        {
            "started_at": started_at,
            "finished_at": utc_now(),
            "returncode": completed.returncode,
            "python": sys.executable,
            "command": command,
            "environment_override_keys": sorted((env_overrides or {}).keys()),
        },
    )
    if completed.returncode != 0:
        tail = (completed.stderr or completed.stdout)[-1200:]
        raise LegacyProcessError(
            f"legacy process failed with exit code {completed.returncode}: {tail}"
        )


def default_legacy_root(module_file: str) -> Path:
    configured = os.environ.get("LAOKUOYANG_ROOT", "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    repository_root = Path(module_file).resolve().parents[3]
    return (repository_root.parent / "laokuoyang").resolve()
