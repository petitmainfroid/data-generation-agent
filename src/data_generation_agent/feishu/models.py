from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Mapping, Protocol, runtime_checkable


JsonValue = Any


def source_content_hash(
    *,
    source_id: str,
    table_id: str,
    view_id: str | None,
    base_record_id: str,
    source_record_id: str,
    fields_json: str,
) -> str:
    fields = json.loads(fields_json)
    if not isinstance(fields, dict):
        raise ValueError("normalized source fields must be a JSON object")
    canonical_fields = json.dumps(
        fields, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )
    if canonical_fields != fields_json:
        raise ValueError("normalized source fields must use canonical JSON")
    payload = json.dumps(
        {
            "source_id": source_id,
            "table_id": table_id,
            "view_id": view_id,
            "base_record_id": base_record_id,
            "source_record_id": source_record_id,
            "fields": fields,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class FeishuIngestionError(RuntimeError):
    """Base ingestion could not produce a complete, trusted snapshot."""


class SourceRegistrationError(FeishuIngestionError):
    """A source allowlist entry is missing or invalid."""


class UnknownSourceError(FeishuIngestionError):
    """A caller requested a source that is not registered."""


class BaseReadError(FeishuIngestionError):
    """A bounded read failed without exposing response bodies or credentials."""


class PaginationError(FeishuIngestionError):
    """Base pagination stopped making progress or exceeded its bound."""


class BaseRequestError(RuntimeError):
    """Sanitized transport error emitted by a concrete Base client."""

    def __init__(
        self,
        status_code: int,
        *,
        error_code: str | None = None,
        retry_after_seconds: float | None = None,
    ) -> None:
        self.status_code = int(status_code)
        self.error_code = error_code
        self.retry_after_seconds = retry_after_seconds
        suffix = f" code={error_code}" if error_code else ""
        super().__init__(f"Base request failed with HTTP {self.status_code}{suffix}")


@dataclass(frozen=True)
class BaseRecord:
    record_id: str
    revision: str | int | None
    fields: Mapping[str, Any]

    def __post_init__(self) -> None:
        object.__setattr__(self, "fields", MappingProxyType(dict(self.fields)))


@dataclass(frozen=True)
class BasePage:
    records: tuple[BaseRecord, ...]
    has_more: bool

    def __post_init__(self) -> None:
        object.__setattr__(self, "records", tuple(self.records))


@runtime_checkable
class BaseClient(Protocol):
    """Read-only Base boundary. No create, update, or delete method is exposed."""

    def list_records(
        self,
        *,
        base_token: str,
        table_id: str,
        view_id: str | None,
        field_ids: tuple[str, ...],
        offset: int,
        limit: int,
    ) -> BasePage: ...


def _required_text(value: str, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SourceRegistrationError(f"{label} must not be empty")
    return value.strip()


@dataclass(frozen=True, repr=False)
class SourceRegistration:
    source_id: str
    base_token: str = field(repr=False)
    table_id: str = ""
    view_id: str | None = None
    field_allowlist: Mapping[str, str] = field(default_factory=dict)
    question_key: str = "question"
    identity_key: str | None = "source_id"
    page_size: int = 100

    def __post_init__(self) -> None:
        source_id = _required_text(self.source_id, "source_id")
        token = _required_text(self.base_token, "base_token")
        table_id = _required_text(self.table_id, "table_id")
        view_id = None if self.view_id is None else _required_text(self.view_id, "view_id")
        if isinstance(self.page_size, bool) or not isinstance(self.page_size, int):
            raise SourceRegistrationError("page_size must be an integer")
        if self.page_size <= 0 or self.page_size > 200:
            raise SourceRegistrationError("page_size must be between 1 and 200")

        allowlist = dict(self.field_allowlist)
        if not allowlist:
            raise SourceRegistrationError("field_allowlist must not be empty")
        for canonical_key, field_id in allowlist.items():
            _required_text(canonical_key, "canonical field key")
            _required_text(field_id, f"field id for {canonical_key}")
        if len(set(allowlist.values())) != len(allowlist):
            raise SourceRegistrationError("field_allowlist field IDs must be unique")
        if self.question_key not in allowlist:
            raise SourceRegistrationError("question_key must be present in field_allowlist")
        if self.identity_key is not None and self.identity_key not in allowlist:
            raise SourceRegistrationError("identity_key must be present in field_allowlist")

        object.__setattr__(self, "source_id", source_id)
        object.__setattr__(self, "base_token", token)
        object.__setattr__(self, "table_id", table_id)
        object.__setattr__(self, "view_id", view_id)
        object.__setattr__(self, "field_allowlist", MappingProxyType(allowlist))

    @property
    def allowed_field_ids(self) -> tuple[str, ...]:
        return tuple(self.field_allowlist[key] for key in sorted(self.field_allowlist))

    def public_payload(self) -> dict[str, JsonValue]:
        private_registration = json.dumps(
            {
                "source_id": self.source_id,
                "table_id": self.table_id,
                "view_id": self.view_id,
                "field_allowlist": {
                    key: self.field_allowlist[key]
                    for key in sorted(self.field_allowlist)
                },
                "question_key": self.question_key,
                "identity_key": self.identity_key,
                "page_size": self.page_size,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return {
            "source_id": self.source_id,
            "registration_digest": hashlib.sha256(
                private_registration.encode("utf-8")
            ).hexdigest(),
            "field_keys": sorted(self.field_allowlist),
            "view_scoped": self.view_id is not None,
            "question_key": self.question_key,
            "identity_key": self.identity_key,
            "page_size": self.page_size,
        }

    def __repr__(self) -> str:
        return (
            f"SourceRegistration(source_id={self.source_id!r}, "
            f"field_keys={tuple(sorted(self.field_allowlist))!r}, "
            f"view_scoped={self.view_id is not None!r}, page_size={self.page_size!r})"
        )


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 3
    base_delay_seconds: float = 0.25
    max_delay_seconds: float = 5.0

    def __post_init__(self) -> None:
        if isinstance(self.max_attempts, bool) or not isinstance(self.max_attempts, int):
            raise ValueError("max_attempts must be an integer")
        if self.max_attempts < 1 or self.max_attempts > 10:
            raise ValueError("max_attempts must be between 1 and 10")
        if self.base_delay_seconds < 0 or self.max_delay_seconds < 0:
            raise ValueError("retry delays must be non-negative")
        if self.base_delay_seconds > self.max_delay_seconds:
            raise ValueError("base_delay_seconds must not exceed max_delay_seconds")


@dataclass(frozen=True)
class AcceptedSourceRecord:
    source_record_id: str
    base_record_id: str
    record_revision: str
    question: str
    fields_json: str
    content_hash: str

    def to_payload(self) -> dict[str, JsonValue]:
        return {
            "source_record_id": self.source_record_id,
            "base_record_id": self.base_record_id,
            "record_revision": self.record_revision,
            "question": self.question,
            "fields_json": self.fields_json,
            "content_hash": self.content_hash,
        }


@dataclass(frozen=True)
class RejectedSourceRecord:
    source_record_id: str | None
    base_record_id: str
    record_revision: str | None
    issue_codes: tuple[str, ...]
    raw_content_hash: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "issue_codes", tuple(sorted(set(self.issue_codes))))

    def to_payload(self) -> dict[str, JsonValue]:
        return {
            "source_record_id": self.source_record_id,
            "base_record_id": self.base_record_id,
            "record_revision": self.record_revision,
            "issue_codes": list(self.issue_codes),
            "raw_content_hash": self.raw_content_hash,
        }


@dataclass(frozen=True)
class IngestionSnapshot:
    schema_version: str
    snapshot_id: str
    source_id: str
    table_id: str
    view_id: str | None
    start_offset: int
    end_offset: int
    page_count: int
    source_record_count: int
    accepted: tuple[AcceptedSourceRecord, ...]
    rejected: tuple[RejectedSourceRecord, ...]
    payload_json: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "accepted", tuple(self.accepted))
        object.__setattr__(self, "rejected", tuple(self.rejected))
