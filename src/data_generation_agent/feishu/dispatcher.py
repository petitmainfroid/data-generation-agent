from __future__ import annotations

import hashlib
import json
import re
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping, Protocol, runtime_checkable

from data_generation_agent.harness.outbox import (
    OutboxLeaseError,
    OutboxRecord,
    OutboxStore,
)
from data_generation_agent.tools.common import redact_secrets


class FeishuDispatchError(RuntimeError):
    """A frozen Outbox write could not be dispatched safely."""


class InvalidFeishuDestinationError(FeishuDispatchError):
    """An Outbox destination is outside the Feishu alias boundary."""


class InvalidFeishuPayloadError(FeishuDispatchError):
    """An Outbox payload is not the narrow Feishu record-write shape."""


@dataclass(frozen=True)
class FeishuWriteResult:
    """Writer result. An ACK requires both booleans to be true."""

    success: bool
    readback_verified: bool
    response: Any = None
    error: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.success, bool):
            raise TypeError("success must be boolean")
        if not isinstance(self.readback_verified, bool):
            raise TypeError("readback_verified must be boolean")
        if self.error is not None and not isinstance(self.error, str):
            raise TypeError("error must be a string when provided")


@runtime_checkable
class FeishuWriter(Protocol):
    """Injected deterministic writer; no model or prompt method is exposed."""

    def write(
        self,
        *,
        base_alias: str,
        table_alias: str,
        operation: str,
        record_id: str | None,
        fields: Mapping[str, Any],
        delivery_key: str,
    ) -> FeishuWriteResult: ...


_ALIAS = r"[a-z][a-z0-9_-]*"
_DESTINATION = re.compile(rf"^feishu:({_ALIAS}):({_ALIAS})$")
_FIELD_ALIAS = re.compile(r"^[a-z][a-z0-9_]*$")


def _canonical_payload_hash(payload: Mapping[str, Any]) -> str:
    try:
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise InvalidFeishuPayloadError("payload must be JSON serializable") from exc
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _utc(value: datetime | str | None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise OutboxLeaseError("dispatcher timestamp is invalid") from exc
    else:
        raise TypeError("now must be datetime, ISO string, or None")
    if parsed.tzinfo is None:
        raise OutboxLeaseError("dispatcher timestamp must include a timezone")
    return parsed.astimezone(timezone.utc)


class FeishuOutboxDispatcher:
    """Claim and deliver frozen Feishu writes from ``OutboxStore``.

    Writer failures and failed readback verification only transition the same
    Outbox row to retry/failed. A successful remote write followed by a lost
    local ACK intentionally remains leased so it can later be reclaimed with
    the same ``delivery_key``.
    """

    def __init__(
        self,
        store: OutboxStore,
        writer: FeishuWriter,
        *,
        max_attempts: int = 3,
        retry_delay_seconds: float = 0,
        max_retry_delay_seconds: float = 60,
    ) -> None:
        if not isinstance(max_attempts, int) or isinstance(max_attempts, bool):
            raise TypeError("max_attempts must be an integer")
        if max_attempts <= 0:
            raise ValueError("max_attempts must be positive")
        if retry_delay_seconds < 0 or max_retry_delay_seconds < 0:
            raise ValueError("retry delays must be non-negative")
        if retry_delay_seconds > max_retry_delay_seconds:
            raise ValueError(
                "retry_delay_seconds must not exceed max_retry_delay_seconds"
            )
        self.store = store
        self.writer = writer
        self.max_attempts = max_attempts
        self.retry_delay_seconds = float(retry_delay_seconds)
        self.max_retry_delay_seconds = float(max_retry_delay_seconds)

    def run_once(
        self,
        worker_id: str,
        *,
        limit: int = 1,
        lease_seconds: float = 60,
        now: datetime | str | None = None,
    ) -> tuple[OutboxRecord, ...]:
        claimed = self.store.claim(
            worker_id,
            limit=limit,
            lease_seconds=lease_seconds,
            now=now,
        )
        return tuple(self.dispatch_claimed(record, now=now) for record in claimed)

    def dispatch_claimed(
        self,
        record: OutboxRecord,
        *,
        now: datetime | str | None = None,
    ) -> OutboxRecord:
        self._assert_current_lease(record, now=now)
        try:
            base_alias, table_alias = self._destination(record.destination)
            record_id, fields = self._payload(record)
        except (InvalidFeishuDestinationError, InvalidFeishuPayloadError) as exc:
            return self._retry(record, type(exc).__name__, exc, now=now)

        try:
            result = self.writer.write(
                base_alias=base_alias,
                table_alias=table_alias,
                operation=record.operation,
                record_id=record_id,
                fields=fields,
                delivery_key=record.delivery_key,
            )
        except Exception as exc:
            return self._retry(record, "WRITER_EXCEPTION", exc, now=now)

        if not isinstance(result, FeishuWriteResult):
            return self._retry(
                record,
                "INVALID_WRITER_RESULT",
                TypeError("writer returned an invalid result object"),
                now=now,
            )
        if not result.success:
            return self._retry(
                record,
                "WRITER_REJECTED",
                result.error or "writer reported failure",
                now=now,
            )
        if not result.readback_verified:
            return self._retry(
                record,
                "READBACK_NOT_VERIFIED",
                result.error or "remote readback did not match the frozen write",
                now=now,
            )

        # Do not catch or convert ACK failures. The remote write has already
        # succeeded, so retry() with the current lease would be unsafe. Lease
        # expiry will reclaim this exact row and delivery key after a lost ACK.
        return self.store.ack(
            record.outbox_id,
            record.lease_token or "",
            response=self._ack_response(result),
            now=now,
        )

    def _assert_current_lease(
        self,
        record: OutboxRecord,
        *,
        now: datetime | str | None,
    ) -> None:
        current = self.store.get(record.outbox_id)
        if (
            current is None
            or current.status != "IN_FLIGHT"
            or not record.lease_token
            or current.lease_token != record.lease_token
            or current.lease_owner != record.lease_owner
        ):
            raise OutboxLeaseError(
                f"Outbox dispatcher lease is stale: {record.outbox_id}"
            )
        if current.lease_expires_at is None or _utc(current.lease_expires_at) <= _utc(now):
            raise OutboxLeaseError(
                f"Outbox dispatcher lease has expired: {record.outbox_id}"
            )

    @staticmethod
    def _destination(destination: str) -> tuple[str, str]:
        if not isinstance(destination, str):
            raise InvalidFeishuDestinationError("destination must be a string")
        match = _DESTINATION.fullmatch(destination)
        if match is None:
            raise InvalidFeishuDestinationError(
                "destination must be feishu:<base_alias>:<table_alias>"
            )
        return match.group(1), match.group(2)

    @staticmethod
    def _payload(record: OutboxRecord) -> tuple[str | None, Mapping[str, Any]]:
        payload = record.payload
        if _canonical_payload_hash(payload) != record.payload_hash:
            raise InvalidFeishuPayloadError("payload hash does not match frozen Outbox row")
        if set(payload) not in ({"fields"}, {"record_id", "fields"}):
            raise InvalidFeishuPayloadError(
                "payload may contain only optional record_id and fields"
            )
        record_id = payload.get("record_id")
        if record_id is not None and (
            not isinstance(record_id, str) or not record_id.strip()
        ):
            raise InvalidFeishuPayloadError("record_id must be a non-empty string")
        raw_fields = payload.get("fields")
        if not isinstance(raw_fields, Mapping) or not raw_fields:
            raise InvalidFeishuPayloadError("fields must be a non-empty object")
        for alias in raw_fields:
            if not isinstance(alias, str) or _FIELD_ALIAS.fullmatch(alias) is None:
                raise InvalidFeishuPayloadError(
                    "fields keys must be registered-style logical aliases"
                )
        # The database row remains the frozen source of truth. Give the writer
        # an isolated JSON-shaped copy so a buggy implementation cannot mutate
        # the claimed record, while normal serializers still receive dict/list.
        return record_id, deepcopy(dict(raw_fields))

    @staticmethod
    def _ack_response(result: FeishuWriteResult) -> Any:
        if result.response is None:
            return {"readback_verified": True}
        if isinstance(result.response, Mapping):
            response = dict(result.response)
            response["readback_verified"] = True
            return response
        return {
            "readback_verified": True,
            "writer_response": result.response,
        }

    def _retry(
        self,
        record: OutboxRecord,
        code: str,
        error: Exception | str,
        *,
        now: datetime | str | None,
    ) -> OutboxRecord:
        detail = redact_secrets(str(error))[:2000]
        safe_error = f"{code}: {detail}" if detail else code
        exponent = max(record.attempt_count - 1, 0)
        delay = min(
            self.retry_delay_seconds * (2**exponent),
            self.max_retry_delay_seconds,
        )
        return self.store.retry(
            record.outbox_id,
            record.lease_token or "",
            safe_error,
            delay_seconds=delay,
            max_attempts=self.max_attempts,
            now=now,
        )
