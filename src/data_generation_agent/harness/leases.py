from __future__ import annotations

import math
import sqlite3
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone

from .db import transaction
from .store import Clock, clock_timestamp, system_clock


class LeaseError(RuntimeError):
    """Base class for durable lease failures."""


class LeaseBusyError(LeaseError):
    """Another owner currently holds an unexpired lease."""


class StaleLeaseTokenError(LeaseError):
    """The supplied fencing token is absent, replaced, or expired."""


@dataclass(frozen=True)
class Lease:
    resource_type: str
    resource_id: str
    owner_id: str
    lease_token: str
    acquired_at: str
    heartbeat_at: str
    expires_at: str
    state_version: int


def _parse_timestamp(value: str) -> float:
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).timestamp()


def _future_timestamp(now_epoch: float, ttl_seconds: float) -> str:
    expires = datetime.fromtimestamp(now_epoch + ttl_seconds, tz=timezone.utc)
    return expires.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _positive_ttl(value: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError("ttl_seconds must be numeric")
    ttl = float(value)
    if not math.isfinite(ttl) or ttl <= 0:
        raise ValueError("ttl_seconds must be finite and positive")
    return ttl


def _required(value: str, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must not be empty")
    return value


def _row_to_lease(row: sqlite3.Row) -> Lease:
    return Lease(
        resource_type=str(row["resource_type"]),
        resource_id=str(row["resource_id"]),
        owner_id=str(row["owner_id"]),
        lease_token=str(row["lease_token"]),
        acquired_at=str(row["acquired_at"]),
        heartbeat_at=str(row["heartbeat_at"]),
        expires_at=str(row["expires_at"]),
        state_version=int(row["state_version"]),
    )


class LeaseManager:
    """SQLite-backed leases with fencing tokens and deterministic clocks."""

    def __init__(
        self,
        connection: sqlite3.Connection,
        *,
        clock: Clock = system_clock,
        token_factory: Callable[[], str] | None = None,
    ) -> None:
        self.connection = connection
        self.clock = clock
        self.token_factory = token_factory or (lambda: f"lease_{uuid.uuid4().hex}")

    def acquire(
        self,
        resource_type: str,
        resource_id: str,
        owner_id: str,
        ttl_seconds: float,
    ) -> Lease:
        resource_type = _required(resource_type, "resource_type")
        resource_id = _required(resource_id, "resource_id")
        owner_id = _required(owner_id, "owner_id")
        ttl = _positive_ttl(ttl_seconds)
        now_epoch, now_text = clock_timestamp(self.clock)
        expires_at = _future_timestamp(now_epoch, ttl)

        with transaction(self.connection, mode="IMMEDIATE"):
            existing = self.connection.execute(
                "SELECT * FROM leases WHERE resource_type = ? AND resource_id = ?",
                (resource_type, resource_id),
            ).fetchone()
            if existing is None:
                lease_token = _required(self.token_factory(), "lease_token")
                self.connection.execute(
                    "INSERT INTO leases "
                    "(resource_type, resource_id, owner_id, lease_token, acquired_at, "
                    "heartbeat_at, expires_at, state_version) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, 0)",
                    (
                        resource_type,
                        resource_id,
                        owner_id,
                        lease_token,
                        now_text,
                        now_text,
                        expires_at,
                    ),
                )
            else:
                current = _row_to_lease(existing)
                if _parse_timestamp(current.expires_at) > now_epoch:
                    raise LeaseBusyError(
                        f"lease is active for {resource_type}/{resource_id}; "
                        f"owner={current.owner_id}"
                    )
                lease_token = _required(self.token_factory(), "lease_token")
                changed = self.connection.execute(
                    "UPDATE leases SET owner_id = ?, lease_token = ?, acquired_at = ?, "
                    "heartbeat_at = ?, expires_at = ?, state_version = state_version + 1 "
                    "WHERE resource_type = ? AND resource_id = ? "
                    "AND lease_token = ? AND state_version = ?",
                    (
                        owner_id,
                        lease_token,
                        now_text,
                        now_text,
                        expires_at,
                        resource_type,
                        resource_id,
                        current.lease_token,
                        current.state_version,
                    ),
                )
                if changed.rowcount != 1:
                    raise LeaseBusyError(f"lease changed while acquiring {resource_type}/{resource_id}")

            row = self.connection.execute(
                "SELECT * FROM leases WHERE resource_type = ? AND resource_id = ?",
                (resource_type, resource_id),
            ).fetchone()
            if row is None:  # defensive: the transaction should make this impossible
                raise LeaseError("lease disappeared during acquisition")
            return _row_to_lease(row)

    def renew(
        self,
        resource_type: str,
        resource_id: str,
        lease_token: str,
        ttl_seconds: float,
    ) -> Lease:
        ttl = _positive_ttl(ttl_seconds)
        now_epoch, now_text = clock_timestamp(self.clock)
        expires_at = _future_timestamp(now_epoch, ttl)
        with transaction(self.connection, mode="IMMEDIATE"):
            current = self._assert_valid_locked(
                resource_type,
                resource_id,
                lease_token,
                now_epoch,
            )
            changed = self.connection.execute(
                "UPDATE leases SET heartbeat_at = ?, expires_at = ?, "
                "state_version = state_version + 1 "
                "WHERE resource_type = ? AND resource_id = ? "
                "AND lease_token = ? AND state_version = ?",
                (
                    now_text,
                    expires_at,
                    resource_type,
                    resource_id,
                    lease_token,
                    current.state_version,
                ),
            )
            if changed.rowcount != 1:
                raise StaleLeaseTokenError(
                    f"lease token changed while renewing {resource_type}/{resource_id}"
                )
            row = self.connection.execute(
                "SELECT * FROM leases WHERE resource_type = ? AND resource_id = ?",
                (resource_type, resource_id),
            ).fetchone()
            if row is None:
                raise StaleLeaseTokenError("lease disappeared during renewal")
            return _row_to_lease(row)

    def assert_valid(
        self,
        resource_type: str,
        resource_id: str,
        lease_token: str,
    ) -> Lease:
        now_epoch, _ = clock_timestamp(self.clock)
        row = self.connection.execute(
            "SELECT * FROM leases WHERE resource_type = ? AND resource_id = ?",
            (resource_type, resource_id),
        ).fetchone()
        if row is None:
            raise StaleLeaseTokenError(f"lease does not exist: {resource_type}/{resource_id}")
        lease = _row_to_lease(row)
        if lease.lease_token != lease_token or _parse_timestamp(lease.expires_at) <= now_epoch:
            raise StaleLeaseTokenError(f"lease token is stale: {resource_type}/{resource_id}")
        return lease

    def is_valid(self, resource_type: str, resource_id: str, lease_token: str) -> bool:
        try:
            self.assert_valid(resource_type, resource_id, lease_token)
        except StaleLeaseTokenError:
            return False
        return True

    def release(self, resource_type: str, resource_id: str, lease_token: str) -> None:
        now_epoch, _ = clock_timestamp(self.clock)
        with transaction(self.connection, mode="IMMEDIATE"):
            current = self._assert_valid_locked(
                resource_type,
                resource_id,
                lease_token,
                now_epoch,
            )
            removed = self.connection.execute(
                "DELETE FROM leases WHERE resource_type = ? AND resource_id = ? "
                "AND lease_token = ? AND state_version = ?",
                (
                    resource_type,
                    resource_id,
                    lease_token,
                    current.state_version,
                ),
            )
            if removed.rowcount != 1:
                raise StaleLeaseTokenError(
                    f"lease token changed while releasing {resource_type}/{resource_id}"
                )

    def reclaim_expired(self) -> int:
        _, now_text = clock_timestamp(self.clock)
        with transaction(self.connection, mode="IMMEDIATE"):
            removed = self.connection.execute(
                "DELETE FROM leases WHERE expires_at <= ?",
                (now_text,),
            )
            return int(removed.rowcount)

    def _assert_valid_locked(
        self,
        resource_type: str,
        resource_id: str,
        lease_token: str,
        now_epoch: float,
    ) -> Lease:
        row = self.connection.execute(
            "SELECT * FROM leases WHERE resource_type = ? AND resource_id = ?",
            (resource_type, resource_id),
        ).fetchone()
        if row is None:
            raise StaleLeaseTokenError(f"lease does not exist: {resource_type}/{resource_id}")
        lease = _row_to_lease(row)
        if lease.lease_token != lease_token or _parse_timestamp(lease.expires_at) <= now_epoch:
            raise StaleLeaseTokenError(f"lease token is stale: {resource_type}/{resource_id}")
        return lease
