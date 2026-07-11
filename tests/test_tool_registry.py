from __future__ import annotations

from pathlib import Path

import pytest

from data_generation_agent.harness.registry import (
    DisabledToolError,
    PipelineDisabledError,
    ToolRegistry,
    ToolRegistryError,
    resolve_within,
)


ROOT = Path(__file__).resolve().parents[1]


def test_registry_loads_real_manifests_and_enforces_disabled_tools() -> None:
    registry = ToolRegistry.load(ROOT)
    quality = registry.get("lao_quality_review", require_enabled=True)
    assert quality.qualified_id == "lao_quality_review@1.1.0"
    assert "DEEPSEEK_API_KEY" in quality.environment_allowlist

    prescreen = registry.get("lao_difficulty_prescreen", require_enabled=True)
    assert prescreen.qualified_id == "lao_difficulty_prescreen@1.1.0"
    consistency = registry.get("consistency_review", require_enabled=True)
    assert consistency.qualified_id == "consistency_review@1.0.0"
    assert "DEEPSEEK_API_KEY" in consistency.environment_allowlist
    synthesis = registry.get("answer_synthesis", require_enabled=True)
    assert synthesis.qualified_id == "answer_synthesis@1.0.0"
    passrate = registry.get("qwen_passrate_review", require_enabled=True)
    assert passrate.qualified_id == "qwen_passrate_review@1.0.0"
    with pytest.raises(DisabledToolError):
        registry.get("lao_legacy_gap_difficulty_review", require_enabled=True)


def test_registry_blocks_full_pipeline_while_release_config_is_disabled() -> None:
    registry = ToolRegistry.load(ROOT)
    with pytest.raises(PipelineDisabledError, match="full pipeline is disabled"):
        registry.assert_pipeline_runnable(Path("configs/question_pipeline.yaml"))


def test_resolve_within_rejects_absolute_and_parent_escape(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    assert resolve_within(root, "runs/job") == root / "runs" / "job"
    with pytest.raises(ToolRegistryError, match="escapes"):
        resolve_within(root, "../outside")
    with pytest.raises(ToolRegistryError, match="escapes"):
        resolve_within(root, tmp_path / "outside")


def test_registry_rejects_shell_command_injection(tmp_path: Path) -> None:
    (tmp_path / "tools" / "bad").mkdir(parents=True)
    (tmp_path / "contracts").mkdir()
    (tmp_path / "contracts" / "in.json").write_text("{}", encoding="utf-8")
    (tmp_path / "contracts" / "out.json").write_text("{}", encoding="utf-8")
    (tmp_path / "tools" / "bad" / "tool.yaml").write_text(
        "\n".join(
            [
                "id: bad",
                "version: '1.0.0'",
                "enabled: true",
                "command: 'python -m data_generation_agent.bad; whoami'",
                "input_schema: contracts/in.json",
                "output_schema: contracts/out.json",
            ]
        ),
        encoding="utf-8",
    )
    with pytest.raises(ToolRegistryError, match="command is not approved"):
        ToolRegistry.load(tmp_path)


def test_registry_rejects_callable_placeholder(tmp_path: Path) -> None:
    (tmp_path / "tools" / "future").mkdir(parents=True)
    (tmp_path / "tools" / "future" / "tool.yaml").write_text(
        "\n".join(
            [
                "id: future",
                "version: '0.0.0'",
                "enabled: true",
                "implementation_status: placeholder",
                "command: null",
                "input_schema: null",
                "output_schema: null",
            ]
        ),
        encoding="utf-8",
    )
    with pytest.raises(ToolRegistryError, match="placeholder must be disabled"):
        ToolRegistry.load(tmp_path)
