from __future__ import annotations

import json
import math
import subprocess
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Sequence

from data_generation_agent.tools.common import redact_secrets

from .client import LarkCliBaseClient, _default_runner
from .profile import BaseProfile, UnregisteredResourceError


class FeishuWriteError(RuntimeError):
    """An allowlisted Base write failed without exposing credentials or values."""


class FeishuWriteConflictError(FeishuWriteError):
    """A business key resolved to multiple records or conflicting data."""


class FeishuWritePolicyError(FeishuWriteError):
    """A requested table, field, or CellValue is not writable by policy."""


@dataclass(frozen=True)
class FieldRule:
    kind: str
    nullable: bool = True

    def validate(self, value: Any) -> None:
        if value is None:
            if not self.nullable:
                raise FeishuWritePolicyError("required field value is null")
            return
        if self.kind in {"text", "select", "datetime"}:
            valid = isinstance(value, str)
        elif self.kind == "number":
            valid = (
                isinstance(value, (int, float))
                and not isinstance(value, bool)
                and math.isfinite(float(value))
            )
        elif self.kind == "checkbox":
            valid = isinstance(value, bool)
        elif self.kind == "multi_select":
            valid = isinstance(value, list) and all(isinstance(item, str) for item in value)
        else:
            raise FeishuWritePolicyError("unsupported field rule")
        if not valid:
            raise FeishuWritePolicyError("CellValue does not match the registered field type")


@dataclass(frozen=True)
class TableWriteRule:
    business_key: str
    fields: Mapping[str, FieldRule]


@dataclass(frozen=True)
class FeishuWritePolicy:
    tables: Mapping[str, TableWriteRule]

    def table(self, alias: str) -> TableWriteRule:
        try:
            return self.tables[alias]
        except KeyError as exc:
            raise FeishuWritePolicyError("table alias is not writable") from exc


def development_write_policy() -> FeishuWritePolicy:
    text = lambda nullable=True: FieldRule("text", nullable)
    number = lambda nullable=True: FieldRule("number", nullable)
    select = lambda nullable=True: FieldRule("select", nullable)
    date = lambda nullable=True: FieldRule("datetime", nullable)
    return FeishuWritePolicy(
        tables={
            "seeds": TableWriteRule(
                business_key="sft_id",
                fields={
                    "question": text(False),
                    "sft_id": text(False),
                    "question_type": select(),
                    "reference_answer": text(),
                    "ingestion_status": select(),
                    "encoding_status": select(),
                    "content_hash": text(),
                    "error_reason": text(),
                    "notes": text(),
                    "run_id": text(),
                    "processed_date": date(),
                },
            ),
            "candidates": TableWriteRule(
                business_key="candidate_id",
                fields={
                    "candidate_id": text(False),
                    "parent_seed_id": text(),
                    "question": text(False),
                    "task_mode": select(),
                    "question_type": select(),
                    "revision": number(),
                    "current_stage": select(),
                    "quality_decision": select(),
                    "prescreen_decision": select(),
                    "consistency_decision": select(),
                    "reference_answer": text(),
                    "qwen_passrate": number(),
                    "valid_trials": number(),
                    "final_decision": select(),
                    "issue_codes": text(),
                    "result_id": text(),
                    "batch_id": text(),
                    "run_id": text(),
                    "updated_date": date(),
                },
            ),
            "progress": TableWriteRule(
                business_key="stats_key",
                fields={
                    "stats_key": text(False),
                    "batch_id": text(),
                    "run_id": text(),
                    "task_mode": select(),
                    "question_type": text(),
                    "stage": select(),
                    "pending": number(),
                    "running": number(),
                    "passed": number(),
                    "rejected": number(),
                    "quarantined": number(),
                    "sync_failed": number(),
                    "machine_remaining": number(),
                    "qualified_deficit": number(),
                    "calculated_date": date(),
                },
            ),
        }
    )


@dataclass(frozen=True)
class WriteReceipt:
    table_alias: str
    record_id: str
    business_key: str
    created: bool
    readback_verified: bool


Runner = Callable[..., subprocess.CompletedProcess[str]]


def _cell_matches(rule: FieldRule, expected: Any, actual: Any) -> bool:
    if rule.kind == "select" and isinstance(expected, str):
        candidate = actual
        if isinstance(candidate, (list, tuple)) and len(candidate) == 1:
            candidate = candidate[0]
        if isinstance(candidate, Mapping):
            candidate = candidate.get("name", candidate.get("text"))
        return candidate == expected
    if rule.kind == "multi_select" and isinstance(expected, list):
        if not isinstance(actual, (list, tuple)):
            return False
        normalized = [
            item.get("name", item.get("text")) if isinstance(item, Mapping) else item
            for item in actual
        ]
        return normalized == expected
    if rule.kind == "number" and isinstance(expected, (int, float)) and isinstance(
        actual, (int, float)
    ):
        return float(actual) == float(expected)
    return actual == expected


class LarkCliBaseWriter:
    """Idempotent profile-bound writes with business-key lookup and readback."""

    def __init__(
        self,
        profile: BaseProfile,
        *,
        policy: FeishuWritePolicy | None = None,
        reader: LarkCliBaseClient | None = None,
        runner: Runner | None = None,
        timeout_seconds: float = 30,
    ) -> None:
        self.profile = profile
        self.policy = policy or development_write_policy()
        self.reader = reader or LarkCliBaseClient(profile, timeout_seconds=timeout_seconds)
        self.runner = runner or _default_runner
        self.timeout_seconds = timeout_seconds

    def upsert(
        self,
        table_alias: str,
        fields: Mapping[str, Any],
        *,
        record_id: str | None = None,
    ) -> WriteReceipt:
        table = self.profile.table(table_alias)
        rule = self.policy.table(table_alias)
        values = dict(fields)
        if not values:
            raise FeishuWritePolicyError("write fields must not be empty")
        unknown = set(values) - set(rule.fields)
        if unknown:
            raise FeishuWritePolicyError("write contains a non-allowlisted field alias")
        if rule.business_key not in values:
            raise FeishuWritePolicyError("write is missing its business idempotency key")
        for alias, value in values.items():
            table.field_id(alias)
            rule.fields[alias].validate(value)
        business_value = values[rule.business_key]
        if not isinstance(business_value, str) or not business_value.strip():
            raise FeishuWritePolicyError("business idempotency key must be non-empty text")

        matches = self._find(table_alias, rule.business_key, business_value, tuple(values))
        if len(matches) > 1:
            raise FeishuWriteConflictError("business idempotency key matched multiple records")
        existing_id = matches[0][0] if matches else None
        if record_id is not None and existing_id is not None and record_id != existing_id:
            raise FeishuWriteConflictError("record_id conflicts with the business key")
        target_id = record_id or existing_id
        created = target_id is None

        field_map = {table.field_id(alias): values[alias] for alias in sorted(values)}
        argv = [
            "lark-cli",
            "base",
            "+record-upsert",
            "--base-token",
            self.profile.base_token,
            "--table-id",
            table.table_id,
            "--as",
            self.profile.identity,
            "--json",
            json.dumps(field_map, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            "--format",
            "json",
        ]
        if target_id is not None:
            argv.extend(["--record-id", target_id])
        self._execute_write(argv)

        readback = self._find(table_alias, rule.business_key, business_value, tuple(values))
        if len(readback) != 1:
            raise FeishuWriteConflictError("write readback did not resolve exactly one record")
        readback_id, readback_fields = readback[0]
        for alias, expected in values.items():
            if not _cell_matches(rule.fields[alias], expected, readback_fields.get(alias)):
                raise FeishuWriteConflictError("write readback value mismatch")
        return WriteReceipt(
            table_alias=table_alias,
            record_id=readback_id,
            business_key=business_value,
            created=created,
            readback_verified=True,
        )

    def _find(
        self,
        table_alias: str,
        business_key: str,
        business_value: str,
        projection: Sequence[str],
    ) -> list[tuple[str, dict[str, Any]]]:
        table = self.profile.table(table_alias)
        page = self.reader.list_records(
            table_alias,
            field_aliases=projection,
            filter_equals=(business_key, business_value),
            limit=2,
        )
        if page.has_more:
            raise FeishuWriteConflictError("business key lookup returned more than two records")
        reverse = {field_id: alias for alias, field_id in table.fields.items()}
        found: list[tuple[str, dict[str, Any]]] = []
        for record in page.records:
            fields = record.get("fields")
            if not isinstance(fields, Mapping):
                raise FeishuWriteError("record readback shape is invalid")
            found.append(
                (
                    str(record["record_id"]),
                    {reverse[field_id]: value for field_id, value in fields.items()},
                )
            )
        return found

    def _execute_write(self, argv: list[str]) -> None:
        try:
            completed = self.runner(argv, timeout=self.timeout_seconds)
        except subprocess.TimeoutExpired as exc:
            raise FeishuWriteError("lark-cli Base write timed out") from exc
        except OSError as exc:
            raise FeishuWriteError("lark-cli Base write executable failed") from exc
        stdout = redact_secrets(str(completed.stdout or ""), [self.profile.base_token])
        stderr = redact_secrets(str(completed.stderr or ""), [self.profile.base_token])
        if completed.returncode != 0:
            raise FeishuWriteError(
                f"lark-cli Base write failed with exit code {completed.returncode}: "
                f"{(stderr or stdout or 'no diagnostic')[-500:]}"
            )
        try:
            envelope = json.loads(stdout)
        except json.JSONDecodeError as exc:
            raise FeishuWriteError("lark-cli Base write returned invalid JSON") from exc
        if not isinstance(envelope, Mapping) or envelope.get("ok") is not True:
            raise FeishuWriteError("lark-cli Base write returned an error envelope")
        if envelope.get("identity") != self.profile.identity:
            raise FeishuWriteError("lark-cli Base write identity mismatch")

    def __repr__(self) -> str:
        return (
            f"LarkCliBaseWriter(base_alias={self.profile.alias!r}, "
            f"identity={self.profile.identity!r})"
        )


class LarkCliDispatchWriter:
    """Bridge ``LarkCliBaseWriter`` to the narrow Outbox writer protocol."""

    def __init__(self, writer: LarkCliBaseWriter) -> None:
        self.writer = writer

    def write(
        self,
        *,
        base_alias: str,
        table_alias: str,
        operation: str,
        record_id: str | None,
        fields: Mapping[str, Any],
        delivery_key: str,
    ):
        from .dispatcher import FeishuWriteResult

        if base_alias != self.writer.profile.alias:
            return FeishuWriteResult(False, False, error="base alias is not registered")
        if operation not in {"CREATE", "UPSERT", "PATCH"}:
            return FeishuWriteResult(False, False, error="operation is not supported")
        try:
            receipt = self.writer.upsert(table_alias, fields, record_id=record_id)
        except Exception as exc:
            return FeishuWriteResult(False, False, error=redact_secrets(str(exc)))
        return FeishuWriteResult(
            success=True,
            readback_verified=receipt.readback_verified,
            response={
                "record_id": receipt.record_id,
                "created": receipt.created,
                "delivery_key": delivery_key,
            },
        )

    def __repr__(self) -> str:
        return f"LarkCliDispatchWriter({self.writer!r})"
