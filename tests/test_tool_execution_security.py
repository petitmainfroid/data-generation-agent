from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from data_generation_agent.tools.common import (
    LegacyProcessError,
    ToolContractError,
    build_subprocess_environment,
    redact_secrets,
    run_legacy_process,
)


def test_subprocess_environment_is_allowlisted_and_logs_are_redacted(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "ALLOWED_API_KEY=top-secret-test-value\n"
        "UNSAFE_SECRET=must-not-be-inherited\n"
        "PYTHONPATH=C:\\attacker-controlled\n",
        encoding="utf-8",
    )
    output_dir = tmp_path / "run"
    command = [
        sys.executable,
        "-c",
        (
            "import os; "
            "print(os.getenv('ALLOWED_API_KEY')); "
            "print(os.getenv('UNSAFE_SECRET', 'missing')); "
            "print(os.getenv('PYTHONPATH', 'missing'))"
        ),
    ]
    run_legacy_process(
        command,
        cwd=tmp_path,
        output_dir=output_dir,
        env_file=env_file,
        allowed_env_keys={"ALLOWED_API_KEY"},
        timeout_seconds=10,
    )
    stdout = (output_dir / "legacy.stdout.log").read_text(encoding="utf-8")
    assert "top-secret-test-value" not in stdout
    assert "must-not-be-inherited" not in stdout
    assert "attacker-controlled" not in stdout
    assert stdout.splitlines() == ["[REDACTED]", "missing", "missing"]
    metadata = json.loads((output_dir / "legacy.process.json").read_text(encoding="utf-8"))
    assert metadata["allowed_environment_keys"] == ["ALLOWED_API_KEY"]
    assert "top-secret-test-value" not in json.dumps(metadata)


def test_nonzero_error_and_stderr_are_redacted(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("ALLOWED_API_KEY=fake-secret-on-stderr\n", encoding="utf-8")
    output_dir = tmp_path / "run"
    command = [
        sys.executable,
        "-c",
        "import os,sys; print(os.environ['ALLOWED_API_KEY'], file=sys.stderr); raise SystemExit(7)",
    ]
    with pytest.raises(LegacyProcessError) as captured:
        run_legacy_process(
            command,
            cwd=tmp_path,
            output_dir=output_dir,
            env_file=env_file,
            allowed_env_keys={"ALLOWED_API_KEY"},
            timeout_seconds=10,
        )
    assert "fake-secret-on-stderr" not in str(captured.value)
    assert "fake-secret-on-stderr" not in (output_dir / "legacy.stderr.log").read_text(
        encoding="utf-8"
    )
    assert "[REDACTED]" in str(captured.value)


def test_timeout_is_normalized_to_legacy_process_error(tmp_path: Path) -> None:
    with pytest.raises(LegacyProcessError, match="exceeded timeout"):
        run_legacy_process(
            [sys.executable, "-c", "import time; time.sleep(2)"],
            cwd=tmp_path,
            output_dir=tmp_path / "run",
            allowed_env_keys=set(),
            timeout_seconds=0.05,
        )
    metadata = json.loads((tmp_path / "run" / "legacy.process.json").read_text(encoding="utf-8"))
    assert metadata["timed_out"] is True
    assert metadata["returncode"] is None


def test_unapproved_environment_override_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ToolContractError, match="not allowlisted"):
        build_subprocess_environment(
            env_file=None,
            allowed_env_keys={"ALLOWED_API_KEY"},
            env_overrides={"UNSAFE_SECRET": "value"},
        )


def test_redactor_covers_headers_assignments_and_common_tokens() -> None:
    raw = (
        "Authorization: Bearer abc.def.ghi\n"
        "api_key=plain-secret\n"
        "token: token-value\n"
        "sk-abcdefghijklmnopqrstuvwxyz"
    )
    redacted = redact_secrets(raw)
    for secret in ("abc.def.ghi", "plain-secret", "token-value", "sk-abcdefghijklmnopqrstuvwxyz"):
        assert secret not in redacted
    assert redacted.count("[REDACTED]") == 4
