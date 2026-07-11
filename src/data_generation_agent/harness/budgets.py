from __future__ import annotations

import math
import sqlite3
from dataclasses import dataclass

from .db import transaction
from .store import Clock, clock_timestamp, system_clock


class BudgetError(RuntimeError):
    """Base class for durable budget failures."""


class BudgetNotFoundError(BudgetError):
    """No budget exists for the job and budget type."""


class BudgetAlreadyExistsError(BudgetError):
    """A conflicting budget definition already exists."""


class BudgetExceededError(BudgetError):
    """A reservation would exceed the durable budget limit."""


class InsufficientReservationError(BudgetError):
    """Consumption or release exceeds the currently reserved amount."""


@dataclass(frozen=True)
class Budget:
    job_id: str
    budget_type: str
    unit: str
    limit_amount: float
    reserved_amount: float
    consumed_amount: float
    updated_at: str

    @property
    def available_amount(self) -> float:
        return max(0.0, self.limit_amount - self.reserved_amount - self.consumed_amount)


def _required(value: str, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must not be empty")
    return value


def _amount(value: float, *, allow_zero: bool) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError("budget amount must be numeric")
    amount = float(value)
    if not math.isfinite(amount) or amount < 0 or (not allow_zero and amount == 0):
        qualifier = "non-negative" if allow_zero else "positive"
        raise ValueError(f"budget amount must be finite and {qualifier}")
    return amount


def _row_to_budget(row: sqlite3.Row) -> Budget:
    return Budget(
        job_id=str(row["job_id"]),
        budget_type=str(row["budget_type"]),
        unit=str(row["unit"]),
        limit_amount=float(row["limit_amount"]),
        reserved_amount=float(row["reserved_amount"]),
        consumed_amount=float(row["consumed_amount"]),
        updated_at=str(row["updated_at"]),
    )


class BudgetManager:
    """Atomic reserve/consume/release accounting for one SQLite database."""

    def __init__(
        self,
        connection: sqlite3.Connection,
        *,
        clock: Clock = system_clock,
    ) -> None:
        self.connection = connection
        self.clock = clock

    def create(
        self,
        job_id: str,
        budget_type: str,
        unit: str,
        limit_amount: float,
    ) -> Budget:
        job_id = _required(job_id, "job_id")
        budget_type = _required(budget_type, "budget_type")
        unit = _required(unit, "unit")
        limit_value = _amount(limit_amount, allow_zero=True)
        _, now_text = clock_timestamp(self.clock)
        with transaction(self.connection, mode="IMMEDIATE"):
            existing = self.connection.execute(
                "SELECT * FROM budgets WHERE job_id = ? AND budget_type = ?",
                (job_id, budget_type),
            ).fetchone()
            if existing is not None:
                current = _row_to_budget(existing)
                if current.unit == unit and current.limit_amount == limit_value:
                    return current
                raise BudgetAlreadyExistsError(
                    f"budget already exists with a different definition: "
                    f"{job_id}/{budget_type}"
                )
            self.connection.execute(
                "INSERT INTO budgets "
                "(job_id, budget_type, unit, limit_amount, reserved_amount, "
                "consumed_amount, updated_at) VALUES (?, ?, ?, ?, 0, 0, ?)",
                (job_id, budget_type, unit, limit_value, now_text),
            )
            return self._get_locked(job_id, budget_type)

    def get(self, job_id: str, budget_type: str) -> Budget:
        row = self.connection.execute(
            "SELECT * FROM budgets WHERE job_id = ? AND budget_type = ?",
            (job_id, budget_type),
        ).fetchone()
        if row is None:
            raise BudgetNotFoundError(f"budget not found: {job_id}/{budget_type}")
        return _row_to_budget(row)

    def reserve(self, job_id: str, budget_type: str, amount: float) -> Budget:
        value = _amount(amount, allow_zero=False)
        _, now_text = clock_timestamp(self.clock)
        with transaction(self.connection, mode="IMMEDIATE"):
            changed = self.connection.execute(
                "UPDATE budgets SET reserved_amount = reserved_amount + ?, updated_at = ? "
                "WHERE job_id = ? AND budget_type = ? "
                "AND reserved_amount + consumed_amount + ? <= limit_amount",
                (value, now_text, job_id, budget_type, value),
            )
            if changed.rowcount != 1:
                existing = self.connection.execute(
                    "SELECT 1 FROM budgets WHERE job_id = ? AND budget_type = ?",
                    (job_id, budget_type),
                ).fetchone()
                if existing is None:
                    raise BudgetNotFoundError(f"budget not found: {job_id}/{budget_type}")
                raise BudgetExceededError(
                    f"reservation exceeds budget: {job_id}/{budget_type}, amount={value}"
                )
            return self._get_locked(job_id, budget_type)

    def consume(self, job_id: str, budget_type: str, amount: float) -> Budget:
        """Move an amount from reserved to consumed atomically."""

        value = _amount(amount, allow_zero=False)
        _, now_text = clock_timestamp(self.clock)
        with transaction(self.connection, mode="IMMEDIATE"):
            changed = self.connection.execute(
                "UPDATE budgets SET reserved_amount = reserved_amount - ?, "
                "consumed_amount = consumed_amount + ?, updated_at = ? "
                "WHERE job_id = ? AND budget_type = ? AND reserved_amount >= ?",
                (value, value, now_text, job_id, budget_type, value),
            )
            if changed.rowcount != 1:
                self._raise_missing_or_insufficient(job_id, budget_type, value, "consume")
            return self._get_locked(job_id, budget_type)

    def release(self, job_id: str, budget_type: str, amount: float) -> Budget:
        value = _amount(amount, allow_zero=False)
        _, now_text = clock_timestamp(self.clock)
        with transaction(self.connection, mode="IMMEDIATE"):
            changed = self.connection.execute(
                "UPDATE budgets SET reserved_amount = reserved_amount - ?, updated_at = ? "
                "WHERE job_id = ? AND budget_type = ? AND reserved_amount >= ?",
                (value, now_text, job_id, budget_type, value),
            )
            if changed.rowcount != 1:
                self._raise_missing_or_insufficient(job_id, budget_type, value, "release")
            return self._get_locked(job_id, budget_type)

    def _get_locked(self, job_id: str, budget_type: str) -> Budget:
        row = self.connection.execute(
            "SELECT * FROM budgets WHERE job_id = ? AND budget_type = ?",
            (job_id, budget_type),
        ).fetchone()
        if row is None:
            raise BudgetNotFoundError(f"budget not found: {job_id}/{budget_type}")
        return _row_to_budget(row)

    def _raise_missing_or_insufficient(
        self,
        job_id: str,
        budget_type: str,
        amount: float,
        operation: str,
    ) -> None:
        row = self.connection.execute(
            "SELECT reserved_amount FROM budgets WHERE job_id = ? AND budget_type = ?",
            (job_id, budget_type),
        ).fetchone()
        if row is None:
            raise BudgetNotFoundError(f"budget not found: {job_id}/{budget_type}")
        raise InsufficientReservationError(
            f"cannot {operation} {amount} from reservation "
            f"{float(row['reserved_amount'])}: {job_id}/{budget_type}"
        )
