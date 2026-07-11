from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


ID_KEYS = ("sft_id", "candidate_key", "generated_id", "question_id", "id")


class ToolContractError(RuntimeError):
    """Raised when tool inputs or legacy outputs violate the public contract."""


class LegacyProcessError(ToolContractError):
    """Raised when a legacy subprocess exits unsuccessfully."""


SAFE_INHERITED_ENV_KEYS = frozenset(
    {
        "APPDATA",
        "COMSPEC",
        "HOME",
        "HOMEDRIVE",
        "HOMEPATH",
        "HTTPS_PROXY",
        "HTTP_PROXY",
        "LANG",
        "LC_ALL",
        "LOCALAPPDATA",
        "NO_PROXY",
        "NUMBER_OF_PROCESSORS",
        "OS",
        "PATH",
        "PATHEXT",
        "PROCESSOR_ARCHITECTURE",
        "PROGRAMDATA",
        "PROGRAMFILES",
        "PROGRAMFILES(X86)",
        "PROGRAMW6432",
        "REQUESTS_CA_BUNDLE",
        "SSL_CERT_FILE",
        "SYSTEMDRIVE",
        "SYSTEMROOT",
        "TEMP",
        "TMP",
        "USERPROFILE",
        "WINDIR",
    }
)

_SECRET_NAME_MARKERS = ("KEY", "TOKEN", "SECRET", "PASSWORD", "CREDENTIAL")


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


def build_subprocess_environment(
    *,
    env_file: Path | None,
    allowed_env_keys: set[str] | frozenset[str],
    env_overrides: dict[str, str] | None = None,
) -> tuple[dict[str, str], list[str]]:
    """Build a minimal environment and return secret values for redaction."""

    allowed = {key.upper() for key in allowed_env_keys}
    overrides = env_overrides or {}
    forbidden_overrides = sorted(key for key in overrides if key.upper() not in allowed)
    if forbidden_overrides:
        raise ToolContractError(
            "environment override keys are not allowlisted: " + ", ".join(forbidden_overrides)
        )

    inherited = os.environ
    environment = {
        key: value
        for key, value in inherited.items()
        if key.upper() in SAFE_INHERITED_ENV_KEYS
    }
    configured = load_env_values(env_file)
    for key in sorted(allowed):
        if key in inherited:
            environment[key] = inherited[key]
        if key in configured:
            environment[key] = configured[key]
    environment.update(overrides)
    environment.setdefault("PYTHONIOENCODING", "utf-8")
    environment.setdefault("PYTHONUTF8", "1")

    secret_values: list[str] = []
    for key, value in environment.items():
        if any(marker in key.upper() for marker in _SECRET_NAME_MARKERS) and len(value) >= 4:
            secret_values.append(value)
    return environment, sorted(set(secret_values), key=len, reverse=True)


def redact_secrets(text: str, secret_values: list[str] | tuple[str, ...] = ()) -> str:
    redacted = text or ""
    for value in sorted(set(secret_values), key=len, reverse=True):
        if value:
            redacted = redacted.replace(value, "[REDACTED]")
    patterns = (
        (r"(?i)(authorization\s*[:=]\s*bearer\s+)[^\s\"']+", r"\1[REDACTED]"),
        (r"(?i)((?:api[-_]?key|token|secret|password)\s*[:=]\s*)[^\s\"']+", r"\1[REDACTED]"),
        (r"\bsk-[A-Za-z0-9_-]{8,}\b", "[REDACTED]"),
    )
    for pattern, replacement in patterns:
        redacted = re.sub(pattern, replacement, redacted)
    return redacted


def run_legacy_process(
    command: list[str],
    *,
    cwd: Path,
    output_dir: Path,
    env_file: Path | None = None,
    env_overrides: dict[str, str] | None = None,
    allowed_env_keys: set[str] | frozenset[str] = frozenset(),
    timeout_seconds: int = 3600,
) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    environment, secret_values = build_subprocess_environment(
        env_file=env_file,
        allowed_env_keys=allowed_env_keys,
        env_overrides=env_overrides,
    )
    started_at = utc_now()
    try:
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
        stdout = redact_secrets(completed.stdout, secret_values)
        stderr = redact_secrets(completed.stderr, secret_values)
        returncode: int | None = completed.returncode
        timed_out = False
    except subprocess.TimeoutExpired as exc:
        raw_stdout = exc.stdout.decode("utf-8", errors="replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        raw_stderr = exc.stderr.decode("utf-8", errors="replace") if isinstance(exc.stderr, bytes) else (exc.stderr or "")
        stdout = redact_secrets(raw_stdout, secret_values)
        stderr = redact_secrets(raw_stderr, secret_values)
        returncode = None
        timed_out = True

    (output_dir / "legacy.stdout.log").write_text(stdout, encoding="utf-8")
    (output_dir / "legacy.stderr.log").write_text(stderr, encoding="utf-8")
    atomic_write_json(
        output_dir / "legacy.process.json",
        {
            "started_at": started_at,
            "finished_at": utc_now(),
            "returncode": returncode,
            "timed_out": timed_out,
            "python": sys.executable,
            "command": command,
            "allowed_environment_keys": sorted({key.upper() for key in allowed_env_keys}),
            "environment_override_keys": sorted((env_overrides or {}).keys()),
        },
    )
    if timed_out:
        raise LegacyProcessError(
            f"legacy process exceeded timeout_seconds={timeout_seconds}"
        )
    if returncode != 0:
        tail = (stderr or stdout)[-1200:]
        raise LegacyProcessError(
            f"legacy process failed with exit code {returncode}: {tail}"
        )


def default_legacy_root(module_file: str) -> Path:
    configured = os.environ.get("LAOKUOYANG_ROOT", "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    repository_root = Path(module_file).resolve().parents[3]
    return (repository_root.parent / "laokuoyang").resolve()
