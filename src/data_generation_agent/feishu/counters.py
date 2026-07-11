from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any


STAGE_STATUS_EVENT = "CANDIDATE_STAGE_STATUS"
_STATUSES = frozenset({"pending", "running", "passed", "rejected", "quarantined"})
_REQUIRED_PAYLOAD_KEYS = (
    "batch_id",
    "task_mode",
    "question_type",
    "stage",
    "status",
)
_ALIAS = re.compile(r"^[a-z][a-z0-9_-]*$")


class CounterProjectionError(RuntimeError):
    """Candidate-stage events could not be projected without ambiguity."""


class InvalidCounterEventError(CounterProjectionError):
    """A counter event is missing required, trustworthy data."""


class UnknownStageStatusError(CounterProjectionError):
    """A stage status is outside the released counter vocabulary."""


@dataclass(frozen=True)
class CounterPolicy:
    """Explicit targets used for the two derived progress counters.

    ``machine_target`` is the maximum number of distinct candidates planned for
    each projected batch/mode/type/stage group. ``qualified_target`` is the
    desired number of passed candidates in that group.
    """

    machine_target: int
    qualified_target: int

    def __post_init__(self) -> None:
        for name, value in (
            ("machine_target", self.machine_target),
            ("qualified_target", self.qualified_target),
        ):
            if isinstance(value, bool) or not isinstance(value, int):
                raise TypeError(f"{name} must be an integer")
            if value < 0:
                raise ValueError(f"{name} must be non-negative")


@dataclass(frozen=True, order=True)
class CounterDimensions:
    batch_id: str
    task_mode: str
    question_type: str
    stage: str

    def __post_init__(self) -> None:
        for name in ("batch_id", "task_mode", "question_type", "stage"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip() or value != value.strip():
                raise ValueError(f"{name} must be a normalized non-empty string")

    @property
    def stats_key(self) -> str:
        encoded = _canonical_json(
            {
                "batch_id": self.batch_id,
                "question_type": self.question_type,
                "stage": self.stage,
                "task_mode": self.task_mode,
            }
        )
        return "stats:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class StageCounter:
    dimensions: CounterDimensions
    pending: int
    running: int
    passed: int
    rejected: int
    quarantined: int
    machine_remaining: int
    qualified_deficit: int
    through_event_id: int

    def __post_init__(self) -> None:
        if not isinstance(self.dimensions, CounterDimensions):
            raise TypeError("dimensions must be CounterDimensions")
        for name in (
            "pending",
            "running",
            "passed",
            "rejected",
            "quarantined",
            "machine_remaining",
            "qualified_deficit",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int):
                raise TypeError(f"{name} must be an integer")
            if value < 0:
                raise ValueError(f"{name} must be non-negative")
        if (
            isinstance(self.through_event_id, bool)
            or not isinstance(self.through_event_id, int)
            or self.through_event_id <= 0
        ):
            raise ValueError("through_event_id must be a positive integer")

    @property
    def stats_key(self) -> str:
        return self.dimensions.stats_key

    @property
    def candidate_count(self) -> int:
        return (
            self.pending
            + self.running
            + self.passed
            + self.rejected
            + self.quarantined
        )

    def progress_fields(self) -> dict[str, str | int]:
        """Return only logical aliases registered by the progress table."""

        return {
            "stats_key": self.stats_key,
            "batch_id": self.dimensions.batch_id,
            "task_mode": self.dimensions.task_mode,
            "question_type": self.dimensions.question_type,
            "stage": self.dimensions.stage,
            "pending": self.pending,
            "running": self.running,
            "passed": self.passed,
            "rejected": self.rejected,
            "quarantined": self.quarantined,
            "machine_remaining": self.machine_remaining,
            "qualified_deficit": self.qualified_deficit,
        }


@dataclass(frozen=True)
class ProgressOutboxDescriptor:
    """A frozen logical write ready to be handed to ``OutboxStore.enqueue``."""

    aggregate_type: str
    aggregate_id: str
    destination: str
    operation: str
    dedupe_key: str
    payload: dict[str, Any]


@dataclass(frozen=True)
class _CandidateStageState:
    event_id: int
    candidate_id: str
    dimensions: CounterDimensions
    status: str


def rebuild_stage_counters(
    events: Iterable[Any],
    *,
    policy: CounterPolicy,
) -> tuple[StageCounter, ...]:
    """Rebuild deterministic counters from append-only stage-status events.

    Unrelated event types are ignored. Every relevant event is validated before
    supersession, so an unknown historical status cannot be hidden by a later
    valid event. For each ``(candidate_id, stage)`` pair, the largest event ID
    wins regardless of input order.
    """

    if not isinstance(policy, CounterPolicy):
        raise TypeError("policy must be a CounterPolicy")

    latest: dict[tuple[str, str], _CandidateStageState] = {}
    by_event_id: dict[int, _CandidateStageState] = {}
    for raw_event in events:
        event_type = _event_value(raw_event, "event_type")
        if event_type != STAGE_STATUS_EVENT:
            continue
        state = _parse_stage_event(raw_event)

        duplicate = by_event_id.get(state.event_id)
        if duplicate is not None:
            if duplicate != state:
                raise InvalidCounterEventError(
                    f"event_id identifies conflicting counter events: {state.event_id}"
                )
            continue
        by_event_id[state.event_id] = state

        key = (state.candidate_id, state.dimensions.stage)
        current = latest.get(key)
        if current is None or state.event_id > current.event_id:
            latest[key] = state

    grouped: dict[CounterDimensions, list[_CandidateStageState]] = {}
    for state in latest.values():
        grouped.setdefault(state.dimensions, []).append(state)

    projections: list[StageCounter] = []
    for dimensions in sorted(grouped):
        states = grouped[dimensions]
        counts = Counter(state.status for state in states)
        total = len(states)
        passed = counts["passed"]
        projections.append(
            StageCounter(
                dimensions=dimensions,
                pending=counts["pending"],
                running=counts["running"],
                passed=passed,
                rejected=counts["rejected"],
                quarantined=counts["quarantined"],
                machine_remaining=max(policy.machine_target - total, 0),
                qualified_deficit=max(policy.qualified_target - passed, 0),
                through_event_id=max(state.event_id for state in states),
            )
        )
    return tuple(projections)


def progress_outbox_descriptor(
    counter: StageCounter,
    *,
    base_alias: str,
) -> ProgressOutboxDescriptor:
    """Build a stable, resource-ID-free UPSERT descriptor for the progress table."""

    if not isinstance(counter, StageCounter):
        raise TypeError("counter must be a StageCounter")
    if not isinstance(base_alias, str) or _ALIAS.fullmatch(base_alias) is None:
        raise ValueError("base_alias must be a registered-style logical alias")

    destination = f"feishu:{base_alias}:progress"
    payload: dict[str, Any] = {"fields": counter.progress_fields()}
    material = _canonical_json(
        {
            "destination": destination,
            "operation": "UPSERT",
            "payload": payload,
        }
    )
    dedupe_key = "feishu-progress:" + hashlib.sha256(material.encode("utf-8")).hexdigest()
    return ProgressOutboxDescriptor(
        aggregate_type="progress_stats",
        aggregate_id=counter.stats_key,
        destination=destination,
        operation="UPSERT",
        dedupe_key=dedupe_key,
        payload=payload,
    )


def _parse_stage_event(raw_event: Any) -> _CandidateStageState:
    event_id = _event_value(raw_event, "event_id")
    if isinstance(event_id, bool) or not isinstance(event_id, int) or event_id <= 0:
        raise InvalidCounterEventError("counter event_id must be a positive integer")

    aggregate_type = _event_value(raw_event, "aggregate_type", default=None)
    if aggregate_type is not None and aggregate_type != "candidate":
        raise InvalidCounterEventError(
            "CANDIDATE_STAGE_STATUS must use the candidate aggregate"
        )
    candidate_id = _required_text(
        _event_value(raw_event, "aggregate_id"), "counter aggregate_id"
    )
    payload = _event_payload(raw_event)
    values = {
        key: _required_text(payload.get(key), f"counter payload.{key}")
        for key in _REQUIRED_PAYLOAD_KEYS
    }
    status = values["status"].lower()
    if status not in _STATUSES:
        raise UnknownStageStatusError(f"unknown candidate stage status: {values['status']}")

    return _CandidateStageState(
        event_id=event_id,
        candidate_id=candidate_id,
        dimensions=CounterDimensions(
            batch_id=values["batch_id"],
            task_mode=values["task_mode"],
            question_type=values["question_type"],
            stage=values["stage"],
        ),
        status=status,
    )


_NO_DEFAULT = object()
_MISSING = object()


def _event_value(raw_event: Any, name: str, *, default: Any = _NO_DEFAULT) -> Any:
    if isinstance(raw_event, Mapping):
        if name in raw_event:
            return raw_event[name]
    elif hasattr(raw_event, "keys") and name in raw_event.keys():
        return raw_event[name]
    elif hasattr(raw_event, name):
        return getattr(raw_event, name)
    if default is not _NO_DEFAULT:
        return default
    raise InvalidCounterEventError(f"counter event is missing {name}")


def _event_payload(raw_event: Any) -> Mapping[str, Any]:
    payload = _event_value(raw_event, "payload", default=_MISSING)
    if payload is _MISSING:
        encoded = _event_value(raw_event, "payload_json", default=_MISSING)
        if not isinstance(encoded, str):
            raise InvalidCounterEventError("counter event is missing payload")
        try:
            payload = json.loads(encoded)
        except json.JSONDecodeError as exc:
            raise InvalidCounterEventError("counter payload_json is invalid") from exc
    if not isinstance(payload, Mapping):
        raise InvalidCounterEventError("counter event payload must be an object")
    return payload


def _required_text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise InvalidCounterEventError(f"{label} must be a non-empty string")
    return value.strip()


def _canonical_json(value: Any) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise CounterProjectionError("counter value must be canonical JSON") from exc
