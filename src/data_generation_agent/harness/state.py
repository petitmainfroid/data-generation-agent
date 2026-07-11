from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Iterable, Mapping


class StateMachineError(RuntimeError):
    """Base class for durable state-machine failures."""


class UnknownAggregateError(StateMachineError):
    """The requested aggregate is not backed by an approved state table."""


class InvalidTransitionError(StateMachineError):
    """A transition is not part of the released state graph."""


class StateConflictError(StateMachineError):
    """Compare-and-set failed because the durable state changed."""

    def __init__(
        self,
        aggregate_type: str,
        aggregate_id: str,
        expected: frozenset[str],
        actual: str | None,
    ) -> None:
        self.aggregate_type = aggregate_type
        self.aggregate_id = aggregate_id
        self.expected = expected
        self.actual = actual
        wanted = ", ".join(sorted(expected))
        super().__init__(
            f"state conflict for {aggregate_type}/{aggregate_id}: "
            f"expected one of [{wanted}], actual={actual!r}"
        )


class StateVersionConflictError(StateMachineError):
    """Compare-and-set failed because the aggregate version changed."""

    def __init__(
        self,
        aggregate_type: str,
        aggregate_id: str,
        expected_version: int,
        actual_version: int | None,
    ) -> None:
        self.aggregate_type = aggregate_type
        self.aggregate_id = aggregate_id
        self.expected_version = expected_version
        self.actual_version = actual_version
        super().__init__(
            f"state version conflict for {aggregate_type}/{aggregate_id}: "
            f"expected={expected_version}, actual={actual_version}"
        )


@dataclass(frozen=True)
class AggregateSpec:
    aggregate_type: str
    table: str
    id_column: str
    state_column: str = "status"
    version_column: str | None = "state_version"
    updated_at_column: str | None = "updated_at"


@dataclass(frozen=True)
class TransitionResult:
    aggregate_type: str
    aggregate_id: str
    previous_status: str
    new_status: str
    previous_version: int
    new_version: int
    event_id: int


def _freeze_transitions(
    transitions: Mapping[str, Mapping[str, Iterable[str]]],
) -> Mapping[str, Mapping[str, frozenset[str]]]:
    frozen: dict[str, Mapping[str, frozenset[str]]] = {}
    for aggregate_type, rules in transitions.items():
        frozen[aggregate_type] = MappingProxyType(
            {source: frozenset(targets) for source, targets in rules.items()}
        )
    return MappingProxyType(frozen)


class StateMachine:
    """Pure validation for aggregate state transitions.

    Persistence and compare-and-set are deliberately kept in ``HarnessStore``;
    this class only owns the released transition graph and SQL identifier
    allowlist.
    """

    def __init__(
        self,
        aggregate_specs: Mapping[str, AggregateSpec],
        transitions: Mapping[str, Mapping[str, Iterable[str]]],
    ) -> None:
        self._specs = MappingProxyType(dict(aggregate_specs))
        self._transitions = _freeze_transitions(transitions)

    def spec_for(self, aggregate_type: str) -> AggregateSpec:
        try:
            return self._specs[aggregate_type]
        except KeyError as exc:
            raise UnknownAggregateError(f"unknown aggregate type: {aggregate_type}") from exc

    def allowed_targets(self, aggregate_type: str, current_status: str) -> frozenset[str]:
        self.spec_for(aggregate_type)
        return self._transitions.get(aggregate_type, {}).get(current_status, frozenset())

    def validate(self, aggregate_type: str, current_status: str, new_status: str) -> None:
        allowed = self.allowed_targets(aggregate_type, current_status)
        if new_status not in allowed:
            raise InvalidTransitionError(
                f"invalid transition for {aggregate_type}: "
                f"{current_status!r} -> {new_status!r}"
            )


JOB_TRANSITIONS = {
    "PENDING": {
        "RUNNING",
        "CANCEL_REQUESTED",
        "CANCELLED",
        "BLOCKED",
        "BLOCKED_NOT_IMPLEMENTED",
    },
    "RUNNING": {
        "PAUSED",
        "CANCEL_REQUESTED",
        "COMPLETED",
        "COMPLETED_PARTIAL",
        "FAILED",
        "CANCELLED",
        "BLOCKED_NOT_IMPLEMENTED",
    },
    "PAUSED": {
        "RUNNING",
        "CANCEL_REQUESTED",
        "CANCELLED",
        "BLOCKED",
        "BLOCKED_NOT_IMPLEMENTED",
    },
    "CANCEL_REQUESTED": {"CANCELLED"},
    "FAILED": {"RUNNING", "CANCELLED"},
    "COMPLETED_PARTIAL": {"RUNNING"},
    "BLOCKED": {"PENDING", "RUNNING", "CANCELLED"},
    "BLOCKED_NOT_IMPLEMENTED": {"PENDING", "RUNNING", "CANCELLED"},
}


CANDIDATE_TRANSITIONS = {
    "PENDING": {"RUNNING", "QUEUED", "REJECTED", "QUARANTINED", "FAILED", "CANCELLED"},
    "RUNNING": {"ACCEPTED", "REJECTED", "QUARANTINED", "FAILED", "CANCELLED"},
    "FAILED": {"PENDING", "RUNNING", "CANCELLED"},
    "QUEUED": {"GENERATING", "QUALITY_REVIEW", "CANCELLED"},
    "GENERATING": {"QUALITY_REVIEW", "RETRY_WAIT", "FAILED", "BUDGET_EXHAUSTED"},
    "QUALITY_REVIEW": {
        "DIFFICULTY_PRESCREEN",
        "REPAIRING",
        "REJECTED_QUALITY",
        "QUARANTINED_TOOL_ERROR",
    },
    "DIFFICULTY_PRESCREEN": {
        "CONSISTENCY_REVIEW",
        "REPAIRING",
        "REJECTED_COARSE_POLICY",
        "QUARANTINED_TOOL_ERROR",
    },
    "CONSISTENCY_REVIEW": {
        "ANSWER_SYNTHESIS",
        "REPAIRING",
        "REJECTED_INCONSISTENT",
        "QUARANTINED_TOOL_ERROR",
    },
    "ANSWER_SYNTHESIS": {
        "QWEN_PASSRATE_REVIEW",
        "REPAIRING",
        "QUARANTINED_TOOL_ERROR",
    },
    "QWEN_PASSRATE_REVIEW": {
        "FINAL_GATE",
        "REPAIRING",
        "REJECTED_PASSRATE",
        "QUARANTINED_TOOL_ERROR",
    },
    "FINAL_GATE": {
        "FINAL_ACCEPTED",
        "REJECTED_QUALITY",
        "REJECTED_COARSE_POLICY",
        "REJECTED_INCONSISTENT",
        "REJECTED_PASSRATE",
        "REJECTED_DUPLICATE",
        "QUARANTINED_UNCERTAIN",
        "QUARANTINED_DISAGREEMENT",
        "QUARANTINED_TOOL_ERROR",
    },
    "REPAIRING": {"QUALITY_REVIEW", "BUDGET_EXHAUSTED", "QUARANTINED_UNCERTAIN"},
    "RETRY_WAIT": {"GENERATING", "QUALITY_REVIEW", "QUARANTINED_TOOL_ERROR"},
}


ATTEMPT_TRANSITIONS = {
    "PENDING": {"RUNNING", "CANCELLED", "STALE"},
    "RUNNING": {
        "SUCCEEDED",
        "RETRYABLE_ERROR",
        "TERMINAL_ERROR",
        "CANCELLED",
        "STALE",
    },
    "RETRYABLE_ERROR": {"PENDING", "RUNNING", "TERMINAL_ERROR", "CANCELLED"},
}


DEFAULT_STATE_MACHINE = StateMachine(
    aggregate_specs={
        "job": AggregateSpec("job", "jobs", "job_id"),
        "candidate": AggregateSpec("candidate", "candidates", "candidate_id"),
        "attempt": AggregateSpec("attempt", "attempts", "attempt_id"),
    },
    transitions={
        "job": JOB_TRANSITIONS,
        "candidate": CANDIDATE_TRANSITIONS,
        "attempt": ATTEMPT_TRANSITIONS,
    },
)
