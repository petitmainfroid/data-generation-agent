from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any

import yaml

from .common import (
    ToolContractError,
    atomic_write_json,
    atomic_write_jsonl,
    default_legacy_root,
    file_hash,
    input_hash,
    read_jsonl,
    run_legacy_process,
    stable_result_id,
    utc_now,
    validate_question_rows,
)


TOOL_ID = "lao_quality_review"
TOOL_VERSION = "1.1.0"
QUALITY_ENV_ALLOWLIST = frozenset(
    {"DEEPSEEK_API_KEY", "API_KEY", "OPENAI_API_KEY", "KIMI_API_KEY"}
)

ISSUE_CODE_MAP = {
    "supply_chain_relevance": "OUT_OF_DOMAIN",
    "问题供应链相关性": "OUT_OF_DOMAIN",
    "question_quality_score": "QUESTION_UNCLEAR",
    "question_quality": "QUESTION_UNCLEAR",
    "题面可验收性": "QUESTION_UNVERIFIABLE",
    "non_open_endedness_score": "ANSWER_NOT_DETERMINATE",
    "non_open_endedness": "ANSWER_NOT_DETERMINATE",
    "definitive_answer_score": "ANSWER_NOT_DETERMINATE",
    "parameter_completeness_score": "MISSING_REQUIRED_CONTEXT",
    "material_completeness_score": "MISSING_REQUIRED_CONTEXT",
    "material_completeness": "MISSING_REQUIRED_CONTEXT",
    "setting_reasonableness_score": "UNREALISTIC_SETTING",
    "设定合理性": "UNREALISTIC_SETTING",
    "题面自洽性": "INCONSISTENT_QUESTION",
    "纯数值性": "NOT_NUMERIC_TASK",
    "extraction_task_orientation_score": "NOT_EXTRACTION_TASK",
    "compliance_safety_orientation_score": "NOT_COMPLIANCE_TASK",
    "knowledge_understanding_orientation_score": "NOT_KNOWLEDGE_TASK",
}


def _resolve_definition(definitions: dict[str, Any], path: str) -> Any:
    node: Any = definitions
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            raise ToolContractError(f"unresolved prompt definition: definitions.{path}")
        node = node[part]
    return node


def _prompt_definition_references(value: Any) -> set[str]:
    if not isinstance(value, str):
        return set()
    patterns = (
        r"\{\{\s*definitions\.([A-Za-z0-9_.\u4e00-\u9fff]+)\s*\}\}",
        r"\{definitions\.([A-Za-z0-9_.\u4e00-\u9fff]+)\}",
    )
    refs: set[str] = set()
    for pattern in patterns:
        refs.update(re.findall(pattern, value))
    return refs


def _load_prompt_strict(path: Path) -> dict[str, Any]:
    try:
        prompt = yaml.safe_load(path.read_text(encoding="utf-8-sig"))
    except yaml.YAMLError as exc:
        raise ToolContractError(f"prompt is not strict YAML: {path}: {exc}") from exc
    if not isinstance(prompt, dict):
        raise ToolContractError(f"prompt root must be a mapping: {path}")
    definitions = prompt.get("definitions")
    if not isinstance(definitions, dict):
        raise ToolContractError(f"prompt definitions missing: {path}")
    output_schema = definitions.get("output_schema")
    if not isinstance(output_schema, str):
        raise ToolContractError(f"prompt output_schema must be a literal JSON string: {path}")
    try:
        parsed_schema = json.loads(output_schema)
    except json.JSONDecodeError as exc:
        raise ToolContractError(f"prompt output_schema is not valid JSON: {path}: {exc}") from exc
    if not isinstance(parsed_schema, dict):
        raise ToolContractError(f"prompt output_schema must decode to an object: {path}")
    for field in ("system", "user", "decision_rule"):
        for reference in _prompt_definition_references(prompt.get(field, "")):
            _resolve_definition(definitions, reference)
    return prompt


def quality_paths(legacy_root: Path, config_path: Path | None = None) -> tuple[Path, Path, list[Path]]:
    module_dir = legacy_root / "modules" / "02_quality_shenhe"
    script = module_dir / "run_hecheng_two_stage.py"
    config = config_path or module_dir / "config.yaml"
    if not script.is_file():
        raise ToolContractError(f"quality legacy entrypoint missing: {script}")
    if not config.is_file():
        raise ToolContractError(f"quality config missing: {config}")
    loaded = yaml.safe_load(config.read_text(encoding="utf-8-sig"))
    if not isinstance(loaded, dict):
        raise ToolContractError(f"quality config root must be a mapping: {config}")
    prompt_config = loaded.get("prompts", {})
    prompt_values = [prompt_config.get("stage0")]
    categories = prompt_config.get("categories", {})
    if isinstance(categories, dict):
        prompt_values.extend(categories.values())
    prompts: list[Path] = []
    for raw in prompt_values:
        if not raw:
            raise ToolContractError(f"quality config has an empty prompt path: {config}")
        prompt_path = Path(str(raw))
        if not prompt_path.is_absolute():
            prompt_path = module_dir / prompt_path
        if not prompt_path.is_file():
            raise ToolContractError(f"quality prompt missing: {prompt_path}")
        prompts.append(prompt_path)
    if len(prompts) != 6:
        raise ToolContractError(f"expected 6 active quality prompts, found {len(prompts)}")
    return module_dir, config, prompts


def preflight_quality(legacy_root: Path, config_path: Path | None = None) -> dict[str, Any]:
    module_dir, config, prompts = quality_paths(legacy_root, config_path)
    loaded_prompts = [_load_prompt_strict(path) for path in prompts]
    return {
        "tool_id": TOOL_ID,
        "tool_version": TOOL_VERSION,
        "legacy_root": str(legacy_root),
        "module_dir": str(module_dir),
        "config": str(config),
        "config_hash": file_hash(config),
        "prompt_count": len(prompts),
        "prompt_hashes": {str(path): file_hash(path) for path in prompts},
        "prompt_ids": [str(prompt.get("meta", {}).get("id", "")) for prompt in loaded_prompts],
        "status": "READY",
    }


def build_quality_command(
    *,
    input_path: Path,
    legacy_output_dir: Path,
    legacy_root: Path,
    config_path: Path | None = None,
    limit: int = 0,
) -> tuple[list[str], Path]:
    module_dir, config, _ = quality_paths(legacy_root, config_path)
    command = [
        sys.executable,
        str(module_dir / "run_hecheng_two_stage.py"),
        "--input",
        str(input_path),
        "--output-dir",
        str(legacy_output_dir),
        "--config",
        str(config),
    ]
    if limit > 0:
        command.extend(["--limit", str(limit)])
    return command, module_dir


def _review_decision(review: dict[str, Any]) -> str:
    value = review.get("final_decision", review.get("decision", ""))
    return str(value).strip().upper()


def validate_stage1_review(review: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    decision = _review_decision(review)
    if decision not in {"ACCEPT", "REJECT"}:
        errors.append("missing_or_invalid_decision")
    score_values: list[int] = []
    for key, value in review.items():
        if key.endswith("_score") or key == "supply_chain_relevance":
            if type(value) is not int or value not in {0, 1}:
                errors.append(f"invalid_binary_score:{key}")
            else:
                score_values.append(value)
    nested_scores = review.get("scores")
    if isinstance(nested_scores, dict):
        for key, value in nested_scores.items():
            if type(value) is not int or value not in {0, 1}:
                errors.append(f"invalid_binary_score:{key}")
            else:
                score_values.append(value)
    if score_values:
        derived = "ACCEPT" if all(value == 1 for value in score_values) else "REJECT"
        if decision and decision != derived:
            errors.append(f"decision_score_mismatch:{decision}!={derived}")
    rationales = review.get("score_rationales")
    if rationales is not None and not isinstance(rationales, dict):
        errors.append("invalid_score_rationales")
    return errors


def _map_issue_key(key: str) -> str:
    if key in ISSUE_CODE_MAP:
        return ISSUE_CODE_MAP[key]
    lowered = key.strip().lower()
    if "parameter" in lowered or "completeness" in lowered or "完整" in key:
        return "MISSING_REQUIRED_CONTEXT"
    if "open" in lowered or "definitive" in lowered or "收束" in key:
        return "ANSWER_NOT_DETERMINATE"
    if "quality" in lowered or "可验收" in key:
        return "QUESTION_UNVERIFIABLE"
    if "relevance" in lowered or "相关" in key:
        return "OUT_OF_DOMAIN"
    return "QUALITY_CHECK_FAILED"


def extract_issue_codes(review: dict[str, Any]) -> list[str]:
    codes: set[str] = set()
    for key, value in review.items():
        if (key.endswith("_score") or key == "supply_chain_relevance") and value == 0:
            codes.add(_map_issue_key(key))
    nested_scores = review.get("scores")
    if isinstance(nested_scores, dict):
        for key, value in nested_scores.items():
            if value == 0:
                codes.add(_map_issue_key(str(key)))
    for collection_key in ("failed_checks", "hard_fail_reasons"):
        values = review.get(collection_key, [])
        if isinstance(values, list):
            for value in values:
                codes.add(_map_issue_key(str(value)))
    if _review_decision(review) == "REJECT" and not codes:
        codes.add("QUALITY_REJECTED")
    return sorted(codes)


def _accepted_review(row: dict[str, Any]) -> tuple[list[str], dict[str, Any], list[str]]:
    categories = [str(value) for value in row.get("problem_category_keys", [])]
    reviews = row.get("stage1_reviews", {})
    if not isinstance(reviews, dict):
        return categories, {}, ["stage1_reviews_not_object"]
    validation_errors: list[str] = []
    for category, review in reviews.items():
        if not isinstance(review, dict):
            validation_errors.append(f"review_not_object:{category}")
            continue
        validation_errors.extend(f"{category}:{error}" for error in validate_stage1_review(review))
        if _review_decision(review) != "ACCEPT":
            validation_errors.append(f"accepted_artifact_contains_non_accept:{category}")
    return categories, reviews, validation_errors


def _rejected_review(row: dict[str, Any]) -> tuple[list[str], dict[str, Any], list[str]]:
    category = str(row.get("category") or row.get("problem_category_key") or "")
    review = row.get("review", {})
    errors: list[str] = []
    if not isinstance(review, dict):
        errors.append("review_not_object")
        review = {}
    if row.get("reject_stage") == "stage1" and review:
        errors.extend(validate_stage1_review(review))
    return ([category] if category else []), review, errors


def compile_quality_results(
    *,
    input_path: Path,
    legacy_output_dir: Path,
    normalized_output_dir: Path,
    preflight: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    inputs = validate_question_rows(input_path)
    accepted_rows = read_jsonl(legacy_output_dir / "final" / "accepted_dedup.jsonl")
    rejected_rows = read_jsonl(legacy_output_dir / "final" / "rejected_all.jsonl")
    accepted = {str(row.get("row_id", "")): row for row in accepted_rows if row.get("row_id")}
    rejected: dict[str, list[dict[str, Any]]] = {}
    for row in rejected_rows:
        candidate_id = str(row.get("row_id", ""))
        if candidate_id:
            rejected.setdefault(candidate_id, []).append(row)

    results: list[dict[str, Any]] = []
    for candidate_id, source in inputs:
        source_hash = input_hash(source)
        result_id = stable_result_id(
            TOOL_ID,
            TOOL_VERSION,
            candidate_id,
            source_hash,
            preflight["config_hash"],
            preflight["prompt_hashes"],
            preflight.get("models", {}),
        )
        base = {
            "schema_version": "quality_review_result.v1",
            "tool_id": TOOL_ID,
            "tool_version": TOOL_VERSION,
            "candidate_id": candidate_id,
            "input_hash": source_hash,
            "result_id": result_id,
            "reviewed_at": utc_now(),
            "model_versions": preflight.get("models", {}),
            "config_hash": preflight["config_hash"],
            "prompt_hashes": preflight["prompt_hashes"],
            "legacy_semantics": "two_stage_any_category_accept",
            "passrate": None,
            "artifacts": {
                "legacy_root": str(legacy_output_dir),
                "accepted": str(legacy_output_dir / "final" / "accepted_dedup.jsonl"),
                "rejected": str(legacy_output_dir / "final" / "rejected_all.jsonl"),
            },
        }
        legacy_terminal_rows = (
            ([accepted[candidate_id]] if candidate_id in accepted else [])
            + rejected.get(candidate_id, [])
        )
        stale_rows = [
            row
            for row in legacy_terminal_rows
            if row.get("question") != source.get("question")
        ]
        if stale_rows:
            results.append(
                {
                    **base,
                    "status": "INVALID",
                    "decision": "ERROR",
                    "category_keys": [],
                    "issue_codes": ["STALE_LEGACY_RESULT"],
                    "rationales": {},
                    "review": {},
                    "repairable": None,
                    "error": "legacy quality result question did not match the current input",
                }
            )
            continue
        if candidate_id in accepted and candidate_id in rejected:
            results.append(
                {
                    **base,
                    "status": "INVALID",
                    "decision": "ERROR",
                    "category_keys": [],
                    "issue_codes": ["CONFLICTING_LEGACY_TERMINAL"],
                    "rationales": {},
                    "review": {},
                    "repairable": None,
                    "error": "candidate appeared in both accepted and rejected artifacts",
                }
            )
            continue
        if candidate_id in accepted:
            categories, review, validation_errors = _accepted_review(accepted[candidate_id])
            if validation_errors:
                results.append(
                    {
                        **base,
                        "status": "INVALID",
                        "decision": "ERROR",
                        "category_keys": categories,
                        "issue_codes": ["INVALID_REVIEW_RESPONSE"],
                        "rationales": {},
                        "review": review,
                        "repairable": None,
                        "error": "; ".join(validation_errors),
                    }
                )
            else:
                results.append(
                    {
                        **base,
                        "status": "COMPLETED",
                        "decision": "ACCEPT",
                        "category_keys": categories,
                        "issue_codes": [],
                        "rationales": {
                            key: value.get("score_rationales", {})
                            for key, value in review.items()
                            if isinstance(value, dict)
                        },
                        "review": review,
                        "repairable": False,
                        "error": None,
                    }
                )
            continue
        if candidate_id in rejected:
            legacy_row = rejected[candidate_id][-1]
            categories, review, validation_errors = _rejected_review(legacy_row)
            legacy_error = str(legacy_row.get("error", "") or "")
            if validation_errors or legacy_error:
                results.append(
                    {
                        **base,
                        "status": "INVALID",
                        "decision": "ERROR",
                        "category_keys": categories,
                        "issue_codes": ["LEGACY_REVIEW_ERROR"],
                        "rationales": review.get("score_rationales", {}),
                        "review": review,
                        "repairable": None,
                        "error": "; ".join(validation_errors + ([legacy_error] if legacy_error else [])),
                    }
                )
            else:
                issue_codes = extract_issue_codes(review)
                if legacy_row.get("reject_stage") == "stage0":
                    issue_codes = sorted(set(issue_codes) | {"TASK_TYPE_UNCLASSIFIED"})
                results.append(
                    {
                        **base,
                        "status": "COMPLETED",
                        "decision": "REJECT",
                        "category_keys": categories,
                        "issue_codes": issue_codes,
                        "rationales": review.get("score_rationales", {}),
                        "review": review,
                        "repairable": None,
                        "error": None,
                    }
                )
            continue
        results.append(
            {
                **base,
                "status": "INVALID",
                "decision": "ERROR",
                "category_keys": [],
                "issue_codes": ["MISSING_LEGACY_RESULT"],
                "rationales": {},
                "review": {},
                "repairable": None,
                "error": "legacy quality output did not contain a terminal result",
            }
        )

    counts = Counter(result["decision"] for result in results)
    summary = {
        "tool_id": TOOL_ID,
        "tool_version": TOOL_VERSION,
        "total": len(results),
        "accepted": counts["ACCEPT"],
        "rejected": counts["REJECT"],
        "errors": counts["ERROR"],
        "complete": counts["ERROR"] == 0 and len(results) == len(inputs),
        "generated_at": utc_now(),
    }
    atomic_write_jsonl(normalized_output_dir / "results.jsonl", results)
    atomic_write_json(normalized_output_dir / "summary.json", summary)
    return results, summary


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Agent-safe adapter for lao two-stage quality review.")
    parser.add_argument("--input", type=Path)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--legacy-root", type=Path)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--timeout", type=int, default=3600)
    parser.add_argument("--skip-run", action="store_true")
    parser.add_argument("--preflight-only", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args(argv)


def run(args: argparse.Namespace) -> int:
    legacy_root = (args.legacy_root or default_legacy_root(__file__)).resolve()
    preflight = preflight_quality(legacy_root, args.config)
    config = yaml.safe_load(Path(preflight["config"]).read_text(encoding="utf-8-sig"))
    preflight["models"] = config.get("models", {}) if isinstance(config, dict) else {}
    if args.preflight_only:
        print(json.dumps(preflight, ensure_ascii=False, indent=2))
        return 0
    if args.input is None or args.output_dir is None:
        raise ToolContractError("--input and --output-dir are required unless --preflight-only is used")
    input_path = args.input.resolve()
    output_dir = args.output_dir.resolve()
    inputs = validate_question_rows(input_path)
    legacy_output_dir = output_dir / "legacy"
    command, cwd = build_quality_command(
        input_path=input_path,
        legacy_output_dir=legacy_output_dir,
        legacy_root=legacy_root,
        config_path=args.config,
        limit=args.limit,
    )
    if args.dry_run:
        print(json.dumps({"preflight": preflight, "input_rows": len(inputs), "command": command}, ensure_ascii=False, indent=2))
        return 0
    if not args.skip_run:
        run_legacy_process(
            command,
            cwd=cwd,
            output_dir=output_dir,
            env_file=args.env_file,
            allowed_env_keys=QUALITY_ENV_ALLOWLIST,
            timeout_seconds=args.timeout,
        )
    _, summary = compile_quality_results(
        input_path=input_path,
        legacy_output_dir=legacy_output_dir,
        normalized_output_dir=output_dir,
        preflight=preflight,
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if summary["complete"] else 2


def main() -> None:
    try:
        raise SystemExit(run(parse_args()))
    except (ToolContractError, OSError) as exc:
        print(f"quality tool error: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc


if __name__ == "__main__":
    main()
