from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from data_generation_agent.tools.common import ToolContractError


_SAFE_COMMAND = re.compile(r"^python -m data_generation_agent\.[A-Za-z0-9_.]+$")


class ToolRegistryError(ToolContractError):
    """A manifest or registered path violates the runtime policy."""


class DisabledToolError(ToolRegistryError):
    """A caller attempted to execute a disabled or placeholder tool."""


class PipelineDisabledError(ToolRegistryError):
    """The complete pipeline is not released for execution."""


def resolve_within(root: Path, candidate: Path | str, *, must_exist: bool = False) -> Path:
    resolved_root = root.resolve()
    raw = Path(candidate)
    resolved = (resolved_root / raw).resolve() if not raw.is_absolute() else raw.resolve()
    try:
        resolved.relative_to(resolved_root)
    except ValueError as exc:
        raise ToolRegistryError(f"path escapes approved root: {candidate}") from exc
    if must_exist and not resolved.exists():
        raise ToolRegistryError(f"approved path does not exist: {candidate}")
    return resolved


@dataclass(frozen=True)
class ToolManifest:
    id: str
    version: str
    enabled: bool
    command: str | None
    input_schema: Path | None
    output_schema: Path | None
    manifest_path: Path
    placeholder: bool
    deprecated: bool
    environment_allowlist: frozenset[str]
    raw: dict[str, Any]

    @property
    def qualified_id(self) -> str:
        return f"{self.id}@{self.version}"


class ToolRegistry:
    def __init__(self, project_root: Path, manifests: dict[str, ToolManifest]) -> None:
        self.project_root = project_root.resolve()
        self._manifests = dict(manifests)

    @classmethod
    def load(cls, project_root: Path) -> "ToolRegistry":
        root = project_root.resolve()
        tools_root = resolve_within(root, "tools", must_exist=True)
        manifests: dict[str, ToolManifest] = {}
        for manifest_path in sorted(tools_root.glob("*/tool.yaml")):
            manifest = cls._load_one(root, manifest_path)
            if manifest.id in manifests:
                raise ToolRegistryError(f"duplicate tool id: {manifest.id}")
            manifests[manifest.id] = manifest
        if not manifests:
            raise ToolRegistryError(f"no tool manifests found under: {tools_root}")
        return cls(root, manifests)

    @staticmethod
    def _load_one(project_root: Path, manifest_path: Path) -> ToolManifest:
        raw = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ToolRegistryError(f"manifest root must be an object: {manifest_path}")
        tool_id = raw.get("id")
        version = raw.get("version")
        enabled = raw.get("enabled")
        if not isinstance(tool_id, str) or not tool_id.strip():
            raise ToolRegistryError(f"manifest id is missing: {manifest_path}")
        if not isinstance(version, str) or not version.strip():
            raise ToolRegistryError(f"manifest version is missing: {manifest_path}")
        if not isinstance(enabled, bool):
            raise ToolRegistryError(f"manifest enabled must be boolean: {manifest_path}")

        placeholder = raw.get("implementation_status") == "placeholder"
        deprecated = raw.get("deprecated") is True
        command = raw.get("command")
        input_raw = raw.get("input_schema")
        output_raw = raw.get("output_schema")

        if placeholder:
            if enabled or command is not None or input_raw is not None or output_raw is not None:
                raise ToolRegistryError(
                    f"placeholder must be disabled and non-callable: {manifest_path}"
                )
        else:
            if not isinstance(command, str) or not _SAFE_COMMAND.fullmatch(command):
                raise ToolRegistryError(f"manifest command is not approved: {manifest_path}")
            if not isinstance(input_raw, str) or not isinstance(output_raw, str):
                raise ToolRegistryError(f"implemented tool schemas are missing: {manifest_path}")

        if deprecated and enabled:
            raise ToolRegistryError(f"deprecated alias cannot be enabled: {manifest_path}")

        def schema_path(value: Any) -> Path | None:
            if value is None:
                return None
            if not isinstance(value, str):
                raise ToolRegistryError(f"schema path must be a string: {manifest_path}")
            return resolve_within(project_root, value, must_exist=True)

        environment = raw.get("environment", {})
        if environment is None:
            environment = {}
        if not isinstance(environment, dict):
            raise ToolRegistryError(f"environment policy must be an object: {manifest_path}")
        allowed = environment.get("allowed", [])
        if not isinstance(allowed, list) or not all(
            isinstance(value, str) and value for value in allowed
        ):
            raise ToolRegistryError(f"environment allowlist is invalid: {manifest_path}")

        return ToolManifest(
            id=tool_id,
            version=version,
            enabled=enabled,
            command=command,
            input_schema=schema_path(input_raw),
            output_schema=schema_path(output_raw),
            manifest_path=manifest_path.resolve(),
            placeholder=placeholder,
            deprecated=deprecated,
            environment_allowlist=frozenset(value.upper() for value in allowed),
            raw=raw,
        )

    def list(self) -> list[ToolManifest]:
        return sorted(self._manifests.values(), key=lambda item: item.id)

    def get(self, tool_id: str, *, require_enabled: bool = False) -> ToolManifest:
        try:
            manifest = self._manifests[tool_id]
        except KeyError as exc:
            raise ToolRegistryError(f"tool is not registered: {tool_id}") from exc
        if require_enabled and (not manifest.enabled or manifest.placeholder or manifest.deprecated):
            raise DisabledToolError(f"tool is not enabled for runtime use: {tool_id}")
        return manifest

    def assert_pipeline_runnable(self, config_path: Path) -> list[ToolManifest]:
        resolved = resolve_within(self.project_root, config_path, must_exist=True)
        config = yaml.safe_load(resolved.read_text(encoding="utf-8"))
        if not isinstance(config, dict):
            raise ToolRegistryError("pipeline config root must be an object")
        if config.get("full_pipeline_enabled") is not True:
            raise PipelineDisabledError("full pipeline is disabled")
        stages = config.get("stages")
        if not isinstance(stages, list) or not stages:
            raise ToolRegistryError("pipeline stages are missing")
        resolved_tools: list[ToolManifest] = []
        for stage in stages:
            if not isinstance(stage, dict):
                raise ToolRegistryError("pipeline stage must be an object")
            if stage.get("enabled") is not True:
                raise PipelineDisabledError(
                    f"required pipeline stage is disabled: {stage.get('stage_id')}"
                )
            qualified = stage.get("tool")
            if not isinstance(qualified, str) or "@" not in qualified:
                raise ToolRegistryError(f"pipeline tool is invalid: {qualified}")
            tool_id, version = qualified.rsplit("@", 1)
            manifest = self.get(tool_id, require_enabled=True)
            if manifest.version != version:
                raise ToolRegistryError(
                    f"pipeline tool version mismatch: {qualified} != {manifest.qualified_id}"
                )
            resolved_tools.append(manifest)
        return resolved_tools
