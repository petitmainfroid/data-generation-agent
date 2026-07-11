"""Canonical entrypoint for the GPT5.5/Qwen difficulty prescreen.

The implementation remains import-compatible with the former
``legacy_gap_difficulty_review`` module while callers migrate to the stage's
correct name.
"""

from .legacy_gap_difficulty_review import (
    PIPELINE_STAGE,
    SCHEMA_VERSION,
    SUBJECTIVE_QWEN_HARD_THRESHOLD,
    TOOL_ID,
    TOOL_VERSION,
    build_difficulty_command,
    compile_difficulty_results,
    difficulty_paths,
    main,
    parse_args,
    preflight_difficulty,
    qwen_gateway_overrides,
    qwen_routing_overrides,
    run,
    validate_judgement,
    validate_prescreen_result,
)

__all__ = [
    "PIPELINE_STAGE",
    "SCHEMA_VERSION",
    "SUBJECTIVE_QWEN_HARD_THRESHOLD",
    "TOOL_ID",
    "TOOL_VERSION",
    "build_difficulty_command",
    "compile_difficulty_results",
    "difficulty_paths",
    "main",
    "parse_args",
    "preflight_difficulty",
    "qwen_gateway_overrides",
    "qwen_routing_overrides",
    "run",
    "validate_judgement",
    "validate_prescreen_result",
]


if __name__ == "__main__":
    main()
