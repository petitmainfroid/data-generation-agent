from __future__ import annotations

import json
import hashlib
import re
import secrets
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from data_generation_agent.tools.common import redact_secrets

from .models import BasePage, BaseRecord, BaseRequestError
from .profile import BaseProfile, TableProfile, UnregisteredResourceError


class LarkCliBaseError(RuntimeError):
    """A read-only lark-cli Base request failed safely."""


class LarkCliTimeoutError(LarkCliBaseError):
    """The fixed read-only command exceeded its deadline."""


class LarkCliCommandError(LarkCliBaseError):
    """lark-cli returned a non-zero exit status or an error envelope."""


class LarkCliRateLimitError(LarkCliCommandError):
    """Feishu or lark-cli reported a retryable rate limit."""


class LarkCliProtocolError(LarkCliBaseError):
    """lark-cli output did not match the JSON envelope contract."""


@dataclass(frozen=True)
class BaseCliEnvelope:
    identity: str
    data: Any
    notice: Any = None


@dataclass(frozen=True)
class RecordPage:
    records: tuple[dict[str, Any], ...]
    record_id_list: tuple[str, ...]
    field_id_list: tuple[str, ...]
    fields: Any
    has_more: bool
    offset: int
    next_offset: int | None
    query_context: Any
    identity: str
    notice: Any = None

    @property
    def cursor(self) -> int | None:
        return self.next_offset


@dataclass(frozen=True)
class FieldPage:
    fields: tuple[dict[str, Any], ...]
    has_more: bool
    offset: int
    next_offset: int | None
    identity: str
    notice: Any = None

    @property
    def cursor(self) -> int | None:
        return self.next_offset


Runner = Callable[..., subprocess.CompletedProcess[str]]
_RATE_LIMIT = re.compile(r"(?i)(?:\bHTTP\s*)?\b429\b|1254291|rate[ _-]?limit|too many requests")
_DIAGNOSTIC_SECRET_KEYS = {
    "api_key",
    "apikey",
    "authorization",
    "cookie",
    "credential",
    "credentials",
    "password",
    "refresh_token",
    "secret",
    "token",
    "access_token",
    "app_secret",
    "client_secret",
}


def _default_runner(
    argv: Sequence[str], *, timeout: float
) -> subprocess.CompletedProcess[str]:
    if not argv or argv[0] != "lark-cli":
        raise OSError("unapproved lark-cli command prefix")
    native_argv = [*_lark_cli_prefix(), *argv[1:]]
    return subprocess.run(
        native_argv,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
        shell=False,
    )


def _lark_cli_prefix() -> list[str]:
    executable = shutil.which("lark-cli")
    if executable is None:
        raise FileNotFoundError("lark-cli is not installed")
    path = Path(executable).resolve()
    if path.suffix.lower() not in {".cmd", ".bat", ".ps1"}:
        return [str(path)]
    script = path.parent / "node_modules" / "@larksuite" / "cli" / "scripts" / "run.js"
    node = path.parent / "node.exe"
    node_executable = str(node) if node.is_file() else shutil.which("node")
    if node_executable is None or not script.is_file():
        raise FileNotFoundError("lark-cli Node.js entrypoint is unavailable")
    return [str(Path(node_executable).resolve()), str(script.resolve())]


def _page_value(value: int, label: str, *, minimum: int, maximum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{label} must be an integer")
    if value < minimum or (maximum is not None and value > maximum):
        upper = "" if maximum is None else f"-{maximum}"
        raise ValueError(f"{label} must be in range {minimum}{upper}")
    return value


def _next_offset(offset: int, count: int, limit: int, has_more: bool) -> int | None:
    if not has_more:
        return None
    return offset + (count if count > 0 else limit)


def _record_rows(
    raw_rows: Any,
    raw_record_ids: Any,
    raw_field_ids: Any,
    raw_fields: Any,
) -> tuple[tuple[dict[str, Any], ...], tuple[str, ...], tuple[str, ...]]:
    """Normalize lark-cli's columnar record-list result.

    Current lark-cli versions return four parallel arrays: ``data`` contains
    row values, ``record_id_list`` identifies rows, and ``field_id_list`` plus
    ``fields`` identify columns. Mapping rows are accepted for compatibility,
    but they are normalized through the same registered field-ID boundary.
    """

    if not isinstance(raw_rows, list):
        raise LarkCliProtocolError("record-list data.data must be a list")
    if not isinstance(raw_record_ids, list) or not all(
        isinstance(value, str) for value in raw_record_ids
    ):
        raise LarkCliProtocolError("record-list record_id_list must be a string list")
    if len(raw_rows) != len(raw_record_ids):
        raise LarkCliProtocolError(
            "record-list data and record_id_list lengths must match"
        )
    if not isinstance(raw_field_ids, list) or not all(
        isinstance(value, str) for value in raw_field_ids
    ):
        raise LarkCliProtocolError("record-list field_id_list must be a string list")
    if len(raw_field_ids) != len(set(raw_field_ids)):
        raise LarkCliProtocolError("record-list field_id_list contains duplicates")
    if not isinstance(raw_fields, (list, tuple)):
        raise LarkCliProtocolError("record-list fields must be a list")
    if len(raw_field_ids) != len(raw_fields):
        raise LarkCliProtocolError(
            "record-list field_id_list and fields lengths must match"
        )

    field_ids = tuple(raw_field_ids)
    normalized: list[dict[str, Any]] = []
    for index, (raw_row, record_id) in enumerate(zip(raw_rows, raw_record_ids)):
        revision_values: dict[str, Any] = {}
        if isinstance(raw_row, Mapping):
            row_record_id = raw_row.get("record_id", record_id)
            if not isinstance(row_record_id, str) or row_record_id != record_id:
                raise LarkCliProtocolError(
                    f"record-list row {index} record_id does not match record_id_list"
                )
            wrapped_fields = raw_row.get("fields")
            if wrapped_fields is None:
                wrapped_fields = raw_row
            if not isinstance(wrapped_fields, Mapping):
                raise LarkCliProtocolError(
                    f"record-list row {index} fields must be an object"
                )
            row_fields = {field_id: wrapped_fields.get(field_id) for field_id in field_ids}
            for key in ("revision", "record_revision", "revision_id"):
                if key in raw_row:
                    revision_values[key] = raw_row[key]
        elif isinstance(raw_row, (list, tuple)):
            if len(raw_row) != len(field_ids):
                raise LarkCliProtocolError(
                    f"record-list row {index} column count does not match field_id_list"
                )
            row_fields = dict(zip(field_ids, raw_row))
        else:
            raise LarkCliProtocolError(
                f"record-list row {index} must be a list or object"
            )
        normalized.append(
            {"record_id": record_id, "fields": row_fields, **revision_values}
        )

    return tuple(normalized), tuple(raw_record_ids), field_ids


def _canonical_revision(record_id: str, fields: Mapping[str, Any]) -> str:
    try:
        payload = json.dumps(
            {"record_id": record_id, "fields": dict(fields)},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise LarkCliProtocolError(
            "record-list row cannot be hashed as canonical JSON"
        ) from exc
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class LarkCliBaseClient:
    """Read-only, alias-bound Base client using fixed lark-cli argv.

    Only ``base +record-list`` and ``base +field-list`` are reachable. The
    profile token is needed in the child argv but is never included in repr,
    exceptions, or returned diagnostics.
    """

    def __init__(
        self,
        profile: BaseProfile,
        *,
        runner: Runner | None = None,
        timeout_seconds: float = 30,
    ) -> None:
        if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float)):
            raise TypeError("timeout_seconds must be numeric")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self.profile = profile
        self._runner = runner or _default_runner
        self.timeout_seconds = float(timeout_seconds)

    def list_records(
        self,
        table_alias: str,
        *,
        field_aliases: Sequence[str] = (),
        view_alias: str | None = None,
        base_alias: str | None = None,
        offset: int = 0,
        limit: int = 100,
    ) -> RecordPage:
        offset = _page_value(offset, "offset", minimum=0)
        limit = _page_value(limit, "limit", minimum=1, maximum=200)
        table = self.profile.table(table_alias, base_alias=base_alias)
        field_ids = [table.field_id(alias) for alias in field_aliases]
        if len(field_ids) != len(set(field_ids)):
            raise ValueError("field_aliases must not contain duplicates")
        view_id = None if view_alias is None else table.view_id(view_alias)

        argv = self._base_argv("+record-list", table.table_id)
        if view_id is not None:
            argv.extend(["--view-id", view_id])
        for field_id in field_ids:
            argv.extend(["--field-id", field_id])
        argv.extend(["--offset", str(offset), "--limit", str(limit), "--format", "json"])
        envelope = self._execute(argv)
        if not isinstance(envelope.data, Mapping):
            raise LarkCliProtocolError("record-list data must be an object")
        raw_records = envelope.data.get("data", [])
        has_more = envelope.data.get("has_more", False)
        if not isinstance(has_more, bool):
            raise LarkCliProtocolError("record-list has_more must be boolean")

        raw_record_ids = envelope.data.get("record_id_list")
        raw_field_ids = envelope.data.get("field_id_list")
        raw_fields = envelope.data.get("fields")
        records, record_ids, response_field_ids = _record_rows(
            raw_records,
            raw_record_ids,
            raw_field_ids,
            raw_fields,
        )

        return RecordPage(
            records=records,
            record_id_list=record_ids,
            field_id_list=response_field_ids,
            fields=tuple(raw_fields),
            has_more=has_more,
            offset=offset,
            next_offset=_next_offset(offset, len(records), limit, has_more),
            query_context=envelope.data.get("query_context"),
            identity=envelope.identity,
            notice=envelope.notice,
        )

    def list_fields(
        self,
        table_alias: str,
        *,
        base_alias: str | None = None,
        offset: int = 0,
        limit: int = 100,
    ) -> FieldPage:
        offset = _page_value(offset, "offset", minimum=0)
        limit = _page_value(limit, "limit", minimum=1, maximum=200)
        table = self.profile.table(table_alias, base_alias=base_alias)
        argv = self._base_argv("+field-list", table.table_id)
        argv.extend(["--offset", str(offset), "--limit", str(limit), "--format", "json"])
        envelope = self._execute(argv)

        if isinstance(envelope.data, list):
            raw_fields = envelope.data
            has_more = False
        elif isinstance(envelope.data, Mapping):
            raw_fields = envelope.data.get(
                "fields", envelope.data.get("data", envelope.data.get("items", []))
            )
            total = envelope.data.get("total")
            if total is not None:
                if isinstance(total, bool) or not isinstance(total, int) or total < 0:
                    raise LarkCliProtocolError(
                        "field-list total must be a non-negative integer"
                    )
                if total < len(raw_fields) if isinstance(raw_fields, list) else False:
                    raise LarkCliProtocolError(
                        "field-list total must not be smaller than the returned page"
                    )
                has_more = offset + len(raw_fields) < total
            else:
                has_more = envelope.data.get("has_more", False)
        else:
            raise LarkCliProtocolError("field-list data must be an object or list")
        if not isinstance(raw_fields, list) or not all(
            isinstance(item, Mapping) for item in raw_fields
        ):
            raise LarkCliProtocolError("field-list fields must be a list of objects")
        if not isinstance(has_more, bool):
            raise LarkCliProtocolError("field-list has_more must be boolean")
        fields = tuple(dict(item) for item in raw_fields)
        return FieldPage(
            fields=fields,
            has_more=has_more,
            offset=offset,
            next_offset=_next_offset(offset, len(fields), limit, has_more),
            identity=envelope.identity,
            notice=envelope.notice,
        )

    def _base_argv(self, shortcut: str, table_id: str) -> list[str]:
        if shortcut not in {"+record-list", "+field-list"}:
            raise LarkCliBaseError("unapproved lark-cli Base shortcut")
        return [
            "lark-cli",
            "base",
            shortcut,
            "--base-token",
            self.profile.base_token,
            "--table-id",
            table_id,
            "--as",
            self.profile.identity,
        ]

    def _execute(self, argv: list[str]) -> BaseCliEnvelope:
        try:
            completed = self._runner(argv, timeout=self.timeout_seconds)
        except subprocess.TimeoutExpired as exc:
            raise LarkCliTimeoutError(
                f"lark-cli Base read timed out after {self.timeout_seconds:g}s"
            ) from exc
        except OSError as exc:
            raise LarkCliCommandError(
                f"lark-cli Base executable failed: {type(exc).__name__}"
            ) from exc

        stdout = self._safe_text(getattr(completed, "stdout", ""))
        stderr = self._safe_text(getattr(completed, "stderr", ""))
        returncode = int(getattr(completed, "returncode", 1))
        combined = f"{stdout}\n{stderr}"
        if returncode != 0:
            diagnostic = (stderr or stdout or "no diagnostic output")[-1000:]
            if _RATE_LIMIT.search(combined):
                raise LarkCliRateLimitError(
                    f"lark-cli Base read was rate limited: {diagnostic}"
                )
            raise LarkCliCommandError(
                f"lark-cli Base command failed with exit code {returncode}: {diagnostic}"
            )

        try:
            parsed = json.loads(stdout)
        except json.JSONDecodeError as exc:
            raise LarkCliProtocolError(
                f"lark-cli Base output was not valid JSON: {stdout[:300]}"
            ) from exc
        if not isinstance(parsed, Mapping):
            raise LarkCliProtocolError("lark-cli Base JSON envelope must be an object")
        if parsed.get("ok") is not True:
            safe_error = self._safe_diagnostic(
                {
                    "code": parsed.get("code"),
                    "error": parsed.get("error"),
                    "message": parsed.get("message"),
                    "notice": parsed.get("_notice"),
                }
            )
            diagnostic = json.dumps(safe_error, ensure_ascii=False, sort_keys=True)
            if _RATE_LIMIT.search(diagnostic):
                raise LarkCliRateLimitError(
                    f"lark-cli Base read was rate limited: {diagnostic[:1000]}"
                )
            raise LarkCliCommandError(
                f"lark-cli Base returned an error envelope: {diagnostic[:1000]}"
            )
        if "data" not in parsed:
            raise LarkCliProtocolError("lark-cli Base JSON envelope is missing data")
        identity = parsed.get("identity", "")
        if not isinstance(identity, str):
            raise LarkCliProtocolError("lark-cli Base identity must be a string")
        return BaseCliEnvelope(
            identity=identity,
            data=parsed["data"],
            notice=self._safe_diagnostic(parsed.get("_notice")),
        )

    def _safe_text(self, value: Any) -> str:
        text = value if isinstance(value, str) else str(value or "")
        return redact_secrets(text, [self.profile.base_token])

    def _safe_diagnostic(self, value: Any) -> Any:
        if isinstance(value, Mapping):
            sanitized: dict[str, Any] = {}
            for key, child in value.items():
                text_key = str(key)
                normalized = text_key.strip().lower().replace("-", "_")
                if normalized in _DIAGNOSTIC_SECRET_KEYS or normalized.endswith(
                    ("_api_key", "_password", "_secret", "_token")
                ):
                    sanitized[text_key] = "[REDACTED]"
                else:
                    sanitized[text_key] = self._safe_diagnostic(child)
            return sanitized
        if isinstance(value, list):
            return [self._safe_diagnostic(child) for child in value]
        if isinstance(value, tuple):
            return tuple(self._safe_diagnostic(child) for child in value)
        if isinstance(value, str):
            return self._safe_text(value)[:1000]
        if value is None or isinstance(value, (bool, int, float)):
            return value
        return self._safe_text(value)[:1000]

    def __repr__(self) -> str:
        return (
            f"LarkCliBaseClient(base_alias={self.profile.alias!r}, "
            f"identity={self.profile.identity!r}, timeout_seconds={self.timeout_seconds!r})"
        )


class LarkCliIngestionClient:
    """Adapt the alias-bound CLI client to the ingestion ``BaseClient`` protocol.

    The ingestion service passes opaque IDs from a ``SourceRegistration``. This
    adapter resolves them back through the local profile and rejects any token,
    table, view, or field combination outside that profile before spawning a
    subprocess.
    """

    def __init__(self, client: LarkCliBaseClient) -> None:
        if not isinstance(client, LarkCliBaseClient):
            raise TypeError("client must be a LarkCliBaseClient")
        self._client = client

    @property
    def profile(self) -> BaseProfile:
        return self._client.profile

    def list_records(
        self,
        *,
        base_token: str,
        table_id: str,
        view_id: str | None,
        field_ids: tuple[str, ...],
        offset: int,
        limit: int,
    ) -> BasePage:
        table_alias, table = self._registered_table(base_token, table_id)
        view_alias = self._registered_view(table, view_id)
        field_aliases = self._registered_fields(table, field_ids)
        try:
            page = self._client.list_records(
                table_alias,
                field_aliases=field_aliases,
                view_alias=view_alias,
                offset=offset,
                limit=limit,
            )
        except LarkCliRateLimitError:
            raise BaseRequestError(429, error_code="lark_cli_rate_limit") from None
        except LarkCliTimeoutError:
            raise BaseRequestError(504, error_code="lark_cli_timeout") from None
        except LarkCliCommandError:
            raise BaseRequestError(502, error_code="lark_cli_command") from None
        except LarkCliProtocolError:
            raise BaseRequestError(502, error_code="lark_cli_protocol") from None

        if set(page.field_id_list) != set(field_ids):
            raise BaseRequestError(502, error_code="lark_cli_field_mismatch")

        allowed = set(field_ids)
        records: list[BaseRecord] = []
        for raw_record in page.records:
            record_id = raw_record.get("record_id")
            raw_record_fields = raw_record.get("fields")
            if not isinstance(record_id, str) or not isinstance(raw_record_fields, Mapping):
                raise BaseRequestError(502, error_code="lark_cli_record_shape")
            if not set(raw_record_fields).issubset(allowed):
                raise BaseRequestError(502, error_code="lark_cli_unallowlisted_field")
            fields = {field_id: raw_record_fields.get(field_id) for field_id in field_ids}
            revision = next(
                (
                    raw_record[key]
                    for key in ("revision", "record_revision", "revision_id")
                    if raw_record.get(key) is not None
                    and str(raw_record.get(key)).strip()
                ),
                None,
            )
            if revision is None:
                revision = _canonical_revision(record_id, fields)
            records.append(
                BaseRecord(record_id=record_id, revision=revision, fields=fields)
            )
        return BasePage(records=tuple(records), has_more=page.has_more)

    def _registered_table(
        self, base_token: str, table_id: str
    ) -> tuple[str, TableProfile]:
        if not isinstance(base_token, str) or not secrets.compare_digest(
            base_token, self.profile.base_token
        ):
            raise UnregisteredResourceError("base token is not registered")
        matches = [
            (alias, table)
            for alias, table in self.profile.tables.items()
            if table.table_id == table_id
        ]
        if len(matches) != 1:
            raise UnregisteredResourceError("table ID is not registered")
        return matches[0]

    @staticmethod
    def _registered_view(table: TableProfile, view_id: str | None) -> str | None:
        if view_id is None:
            return None
        matches = [alias for alias, registered in table.views.items() if registered == view_id]
        if len(matches) != 1:
            raise UnregisteredResourceError(
                f"view ID is not registered for table {table.alias!r}"
            )
        return matches[0]

    @staticmethod
    def _registered_fields(
        table: TableProfile, field_ids: tuple[str, ...]
    ) -> tuple[str, ...]:
        if not isinstance(field_ids, tuple) or not all(
            isinstance(field_id, str) for field_id in field_ids
        ):
            raise UnregisteredResourceError("field IDs must be a tuple of registered IDs")
        if len(field_ids) != len(set(field_ids)):
            raise UnregisteredResourceError("field IDs must not contain duplicates")
        reverse = {field_id: alias for alias, field_id in table.fields.items()}
        try:
            return tuple(reverse[field_id] for field_id in field_ids)
        except KeyError as exc:
            raise UnregisteredResourceError(
                f"field ID is not registered for table {table.alias!r}"
            ) from exc

    def __repr__(self) -> str:
        return (
            f"LarkCliIngestionClient(base_alias={self.profile.alias!r}, "
            f"identity={self.profile.identity!r})"
        )
