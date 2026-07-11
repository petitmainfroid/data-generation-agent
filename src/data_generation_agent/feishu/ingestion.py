from __future__ import annotations

import hashlib
import json
import math
import re
import time
import unicodedata
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Callable, Iterable, Mapping

from .models import (
    AcceptedSourceRecord,
    BaseClient,
    BasePage,
    BaseReadError,
    BaseRecord,
    BaseRequestError,
    IngestionSnapshot,
    PaginationError,
    RejectedSourceRecord,
    RetryPolicy,
    SourceRegistration,
    SourceRegistrationError,
    UnknownSourceError,
    source_content_hash,
)


SNAPSHOT_SCHEMA_VERSION = "feishu_ingestion_snapshot.v1"
_HORIZONTAL_SPACE = re.compile(r"[\t \f\v]+")


class SourceRegistry:
    """Immutable allowlist of approved Base/table/view/field combinations."""

    def __init__(self, registrations: Iterable[SourceRegistration]) -> None:
        registered: dict[str, SourceRegistration] = {}
        for registration in registrations:
            if registration.source_id in registered:
                raise SourceRegistrationError(
                    f"duplicate source registration: {registration.source_id}"
                )
            registered[registration.source_id] = registration
        if not registered:
            raise SourceRegistrationError("at least one source registration is required")
        self._registrations = MappingProxyType(registered)

    def _resolve(self, source_id: str) -> SourceRegistration:
        try:
            return self._registrations[source_id]
        except KeyError as exc:
            raise UnknownSourceError(f"source is not registered: {source_id}") from exc

    def describe(self) -> tuple[dict[str, Any], ...]:
        return tuple(
            self._registrations[source_id].public_payload()
            for source_id in sorted(self._registrations)
        )


@dataclass
class _PreparedRecord:
    base_record_id: str
    source_record_id: str | None
    record_revision: str | None
    question: str | None
    fields_json: str | None
    content_hash: str | None
    raw_content_hash: str
    issues: set[str] = field(default_factory=set)


def _canonical_json(value: Any, *, ensure_ascii: bool = False) -> str:
    return json.dumps(
        value,
        ensure_ascii=ensure_ascii,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _normalize_json_value(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("non-finite numeric field")
        return value
    if isinstance(value, str):
        if _has_encoding_anomaly(value):
            raise UnicodeError("Base field contains an encoding anomaly")
        return unicodedata.normalize("NFKC", value).replace("\r\n", "\n").replace("\r", "\n")
    if isinstance(value, Mapping):
        normalized: dict[str, Any] = {}
        for key, child in value.items():
            if not isinstance(key, str):
                raise ValueError("field object keys must be strings")
            normalized[key] = _normalize_json_value(child)
        return {key: normalized[key] for key in sorted(normalized)}
    if isinstance(value, (list, tuple)):
        return [_normalize_json_value(child) for child in value]
    raise ValueError(f"unsupported Base field value type: {type(value).__name__}")


def _safe_raw_hash(registration: SourceRegistration, record: BaseRecord) -> str:
    allowed = {
        canonical_key: record.fields.get(field_id)
        for canonical_key, field_id in registration.field_allowlist.items()
    }
    try:
        serializable = _normalize_json_value(allowed)
    except (UnicodeError, ValueError):
        serializable = {
            "field_types": {
                key: f"{type(value).__module__}.{type(value).__qualname__}"
                for key, value in sorted(allowed.items())
            }
        }
    payload = {
        "source_id": registration.source_id,
        "table_id": registration.table_id,
        "view_id": registration.view_id,
        "base_record_id": record.record_id,
        "record_revision": record.revision,
        "allowlisted_fields": serializable,
    }
    return _sha256_text(_canonical_json(payload, ensure_ascii=True))


def _extract_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, Mapping):
        for key in ("text", "name"):
            if isinstance(value.get(key), str):
                return str(value[key])
        raise TypeError("Base text object does not contain text or name")
    if isinstance(value, (list, tuple)):
        return "".join(_extract_text(item) for item in value)
    raise TypeError("Base question/identity field is not textual")


def _has_encoding_anomaly(value: str) -> bool:
    if "\ufffd" in value or "\x00" in value or "锟斤拷" in value:
        return True
    for character in value:
        codepoint = ord(character)
        if 0xD800 <= codepoint <= 0xDFFF:
            return True
        if unicodedata.category(character) == "Cc" and character not in "\t\n\r":
            return True
    return False


def normalize_question(value: Any) -> str:
    text = _extract_text(value)
    if _has_encoding_anomaly(text):
        raise UnicodeError("question contains an encoding anomaly")
    text = unicodedata.normalize("NFKC", text).replace("\r\n", "\n").replace("\r", "\n")
    lines = [_HORIZONTAL_SPACE.sub(" ", line).strip() for line in text.split("\n")]
    while lines and not lines[0]:
        lines.pop(0)
    while lines and not lines[-1]:
        lines.pop()
    return "\n".join(lines)


def _normalize_identity(value: Any) -> str:
    identity = unicodedata.normalize("NFKC", _extract_text(value)).strip()
    if _has_encoding_anomaly(identity):
        raise UnicodeError("source identity contains an encoding anomaly")
    return identity


def _prepare_record(
    registration: SourceRegistration,
    record: BaseRecord,
) -> _PreparedRecord:
    issues: set[str] = set()
    base_record_id = record.record_id.strip() if isinstance(record.record_id, str) else ""
    if not base_record_id:
        issues.add("MISSING_RECORD_ID")
    elif _has_encoding_anomaly(base_record_id):
        base_record_id = ""
        issues.add("ENCODING_ANOMALY")

    revision = None if record.revision is None else str(record.revision).strip()
    if not revision:
        revision = None
        issues.add("MISSING_RECORD_REVISION")
    elif _has_encoding_anomaly(revision):
        revision = None
        issues.add("ENCODING_ANOMALY")

    raw_hash = _safe_raw_hash(registration, record)
    normalized_fields: dict[str, Any] = {}
    try:
        for canonical_key, field_id in registration.field_allowlist.items():
            normalized_fields[canonical_key] = _normalize_json_value(record.fields.get(field_id))
    except UnicodeError:
        issues.add("ENCODING_ANOMALY")
    except ValueError:
        issues.add("INVALID_FIELD_VALUE")

    question: str | None = None
    question_field = registration.field_allowlist[registration.question_key]
    try:
        question = normalize_question(record.fields.get(question_field))
        if not question:
            issues.add("EMPTY_QUESTION")
        elif normalized_fields:
            normalized_fields[registration.question_key] = question
    except UnicodeError:
        issues.add("ENCODING_ANOMALY")
    except TypeError:
        issues.add("INVALID_QUESTION")

    source_record_id: str | None
    if registration.identity_key is None:
        source_record_id = base_record_id or None
    else:
        identity_field = registration.field_allowlist[registration.identity_key]
        try:
            source_record_id = _normalize_identity(record.fields.get(identity_field)) or None
            if source_record_id is None:
                issues.add("MISSING_SOURCE_ID")
            elif normalized_fields:
                normalized_fields[registration.identity_key] = source_record_id
        except UnicodeError:
            source_record_id = None
            issues.add("ENCODING_ANOMALY")
        except TypeError:
            source_record_id = None
            issues.add("INVALID_SOURCE_ID")

    fields_json: str | None = None
    content_hash: str | None = None
    if normalized_fields and question and source_record_id and base_record_id:
        try:
            fields_json = _canonical_json(normalized_fields)
            content_hash = source_content_hash(
                source_id=registration.source_id,
                table_id=registration.table_id,
                view_id=registration.view_id,
                base_record_id=base_record_id,
                source_record_id=source_record_id,
                fields_json=fields_json,
            )
        except (TypeError, ValueError, UnicodeError):
            issues.add("INVALID_FIELD_VALUE")
            fields_json = None
            content_hash = None

    return _PreparedRecord(
        base_record_id=base_record_id,
        source_record_id=source_record_id,
        record_revision=revision,
        question=question,
        fields_json=fields_json,
        content_hash=content_hash,
        raw_content_hash=raw_hash,
        issues=issues,
    )


def _mark_duplicate_ids(records: list[_PreparedRecord]) -> None:
    base_counts: dict[str, int] = {}
    source_counts: dict[str, int] = {}
    for record in records:
        if record.base_record_id:
            base_counts[record.base_record_id] = base_counts.get(record.base_record_id, 0) + 1
        if record.source_record_id:
            source_counts[record.source_record_id] = source_counts.get(record.source_record_id, 0) + 1
    for record in records:
        if (
            record.base_record_id
            and base_counts.get(record.base_record_id, 0) > 1
        ) or (
            record.source_record_id
            and source_counts.get(record.source_record_id, 0) > 1
        ):
            record.issues.add("DUPLICATE_ID")


class FeishuIngestionService:
    def __init__(
        self,
        client: BaseClient,
        registry: SourceRegistry,
        *,
        retry_policy: RetryPolicy | None = None,
        sleeper: Callable[[float], None] = time.sleep,
        max_pages: int = 10_000,
    ) -> None:
        if isinstance(max_pages, bool) or not isinstance(max_pages, int) or max_pages < 1:
            raise ValueError("max_pages must be a positive integer")
        self.client = client
        self.registry = registry
        self.retry_policy = retry_policy or RetryPolicy()
        self.sleeper = sleeper
        self.max_pages = max_pages

    def ingest(
        self,
        source_id: str,
        *,
        cursor: int | str | None = None,
        prior_records: Iterable[BaseRecord] = (),
        prior_page_count: int = 0,
        snapshot_start_offset: int | None = None,
        on_page: Callable[[int, int, tuple[BaseRecord, ...]], None] | None = None,
    ) -> IngestionSnapshot:
        registration = self.registry._resolve(source_id)
        offset = self._parse_cursor(cursor)
        start_offset = (
            offset
            if snapshot_start_offset is None
            else self._parse_cursor(snapshot_start_offset)
        )
        if (
            isinstance(prior_page_count, bool)
            or not isinstance(prior_page_count, int)
            or prior_page_count < 0
        ):
            raise PaginationError("prior_page_count must be a non-negative integer")
        records = list(prior_records)
        if offset == start_offset and (records or prior_page_count):
            raise PaginationError("prior records require an advanced resume cursor")
        if offset > start_offset and not records:
            raise PaginationError("advanced resume cursor requires prior records")
        pages = prior_page_count
        pages_read = 0

        while True:
            if pages_read >= self.max_pages:
                raise PaginationError(
                    f"Base pagination exceeded max_pages for registered source {source_id}"
                )
            page = self._read_page(registration, offset)
            if not isinstance(page, BasePage):
                raise BaseReadError("Base client returned an invalid page object")
            pages += 1
            pages_read += 1
            records.extend(page.records)
            next_offset = offset + len(page.records)
            if page.has_more and (not page.records or next_offset <= offset):
                raise PaginationError(
                    f"Base pagination made no progress for registered source {source_id}"
                )
            if on_page is not None and (records or next_offset > start_offset):
                on_page(next_offset, pages, tuple(records))
            if not page.has_more:
                offset = next_offset
                break
            offset = next_offset

        prepared = [_prepare_record(registration, record) for record in records]
        _mark_duplicate_ids(prepared)
        accepted: list[AcceptedSourceRecord] = []
        rejected: list[RejectedSourceRecord] = []
        for record in prepared:
            if record.issues:
                rejected.append(
                    RejectedSourceRecord(
                        source_record_id=record.source_record_id,
                        base_record_id=record.base_record_id,
                        record_revision=record.record_revision,
                        issue_codes=tuple(record.issues),
                        raw_content_hash=record.raw_content_hash,
                    )
                )
                continue
            if not all(
                (
                    record.source_record_id,
                    record.base_record_id,
                    record.record_revision,
                    record.question,
                    record.fields_json,
                    record.content_hash,
                )
            ):
                rejected.append(
                    RejectedSourceRecord(
                        source_record_id=record.source_record_id,
                        base_record_id=record.base_record_id,
                        record_revision=record.record_revision,
                        issue_codes=("INCOMPLETE_NORMALIZED_RECORD",),
                        raw_content_hash=record.raw_content_hash,
                    )
                )
                continue
            accepted.append(
                AcceptedSourceRecord(
                    source_record_id=record.source_record_id,
                    base_record_id=record.base_record_id,
                    record_revision=record.record_revision,
                    question=record.question,
                    fields_json=record.fields_json,
                    content_hash=record.content_hash,
                )
            )

        accepted.sort(key=lambda item: (item.source_record_id, item.base_record_id))
        rejected.sort(
            key=lambda item: (
                item.source_record_id or "",
                item.base_record_id,
                item.record_revision or "",
                item.raw_content_hash,
            )
        )
        payload = {
            "schema_version": SNAPSHOT_SCHEMA_VERSION,
            "source": registration.public_payload(),
            "start_offset": start_offset,
            "end_offset": offset,
            "page_count": pages,
            "source_record_count": len(records),
            "accepted": [record.to_payload() for record in accepted],
            "rejected": [record.to_payload() for record in rejected],
        }
        payload_json = _canonical_json(payload)
        if registration.base_token in payload_json:
            raise BaseReadError(
                f"snapshot contained credential material for registered source {registration.source_id}"
            )
        return IngestionSnapshot(
            schema_version=SNAPSHOT_SCHEMA_VERSION,
            snapshot_id=_sha256_text(payload_json),
            source_id=registration.source_id,
            table_id=registration.table_id,
            view_id=registration.view_id,
            start_offset=start_offset,
            end_offset=offset,
            page_count=pages,
            source_record_count=len(records),
            accepted=tuple(accepted),
            rejected=tuple(rejected),
            payload_json=payload_json,
        )

    def _read_page(self, registration: SourceRegistration, offset: int) -> BasePage:
        policy = self.retry_policy
        for attempt in range(1, policy.max_attempts + 1):
            try:
                return self.client.list_records(
                    base_token=registration.base_token,
                    table_id=registration.table_id,
                    view_id=registration.view_id,
                    field_ids=registration.allowed_field_ids,
                    offset=offset,
                    limit=registration.page_size,
                )
            except BaseRequestError as exc:
                retryable = exc.status_code == 429 or 500 <= exc.status_code <= 599
                if not retryable or attempt >= policy.max_attempts:
                    raise BaseReadError(
                        "Base read failed for registered source "
                        f"{registration.source_id}; status={exc.status_code}; attempts={attempt}"
                    ) from None
                delay = policy.base_delay_seconds * (2 ** (attempt - 1))
                if exc.retry_after_seconds is not None:
                    retry_after = float(exc.retry_after_seconds)
                    if math.isfinite(retry_after) and retry_after >= 0:
                        delay = retry_after
                self.sleeper(min(delay, policy.max_delay_seconds))
            except Exception:
                raise BaseReadError(
                    f"Base client raised an unexpected read error for registered source {registration.source_id}"
                ) from None
        raise AssertionError("bounded retry loop exited unexpectedly")

    @staticmethod
    def _parse_cursor(cursor: int | str | None) -> int:
        if cursor is None:
            return 0
        if isinstance(cursor, bool):
            raise PaginationError("Base offset cursor must be a non-negative integer")
        if isinstance(cursor, int):
            offset = cursor
        elif isinstance(cursor, str) and cursor.strip().isdigit():
            offset = int(cursor.strip())
        else:
            raise PaginationError("Base offset cursor must be a non-negative integer")
        if offset < 0:
            raise PaginationError("Base offset cursor must be a non-negative integer")
        return offset
