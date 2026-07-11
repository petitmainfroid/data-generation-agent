from __future__ import annotations

import hashlib
import json
import math
import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Mapping


class KnowledgeContractError(ValueError):
    """Knowledge, persona, or Prompt input violates its versioned contract."""


def canonical_json(value: Any) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise KnowledgeContractError("value must be canonical JSON") from exc


def digest_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def normalize_text(value: str) -> str:
    if not isinstance(value, str):
        raise KnowledgeContractError("text must be a string")
    if "\ufffd" in value or "\x00" in value:
        raise KnowledgeContractError("text contains an encoding anomaly")
    normalized = unicodedata.normalize("NFKC", value).replace("\r\n", "\n").replace(
        "\r", "\n"
    )
    lines = [line.rstrip() for line in normalized.split("\n")]
    return "\n".join(lines).strip()


_TOKEN = re.compile(r"[A-Za-z0-9_]+|[\u3400-\u9fff]")
_INJECTION = re.compile(
    r"(?i)(ignore (?:all |the )?(?:previous|system)|system prompt|developer message|"
    r"reveal .*(?:api[ _-]?key|secret|token|prompt)|"
    r"忽略(?:之前|以上|系统)|系统提示词|泄露.*(?:密钥|令牌|提示词))"
)

ALLOWED_QUESTION_TYPES = frozenset(
    {
        "multiple_choice",
        "numeric_calculation",
        "logical_reasoning",
        "industry_knowledge",
        "compliance_safety",
        "information_extraction",
        "other",
    }
)


def retrieval_tokens(text: str) -> tuple[str, ...]:
    return tuple(token.lower() for token in _TOKEN.findall(text))


def injection_suspected(text: str) -> bool:
    return _INJECTION.search(text) is not None


@dataclass(frozen=True)
class KnowledgeDocument:
    source_alias: str
    source_revision: str
    title: str
    content: str

    def __post_init__(self) -> None:
        for name in ("source_alias", "source_revision", "title"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise KnowledgeContractError(f"{name} must be non-empty text")
        normalized = normalize_text(self.content)
        if not normalized:
            raise KnowledgeContractError("knowledge content must not be empty")
        object.__setattr__(self, "content", normalized)


@dataclass(frozen=True)
class KnowledgeChunk:
    chunk_id: str
    snapshot_id: str
    sequence_number: int
    citation: str
    text: str
    text_hash: str
    tokens: tuple[str, ...]
    injection_suspected: bool


@dataclass(frozen=True)
class PersonaTemplate:
    persona_id: str
    version: str
    role: str
    objectives: tuple[str, ...]
    constraints: tuple[str, ...]
    allowed_question_types: tuple[str, ...]
    tone: str
    forbidden_behaviors: tuple[str, ...]

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "PersonaTemplate":
        allowed = {
            "schema_version",
            "persona_id",
            "version",
            "role",
            "objectives",
            "constraints",
            "allowed_question_types",
            "tone",
            "forbidden_behaviors",
        }
        if not isinstance(value, Mapping) or set(value) - allowed:
            raise KnowledgeContractError("persona contains unknown fields")
        if value.get("schema_version") != "persona_template.v1":
            raise KnowledgeContractError("unsupported persona schema_version")

        def text(name: str) -> str:
            item = value.get(name)
            if not isinstance(item, str) or not item.strip():
                raise KnowledgeContractError(f"persona {name} must be non-empty text")
            return normalize_text(item)

        def texts(name: str, *, required: bool = True) -> tuple[str, ...]:
            item = value.get(name)
            if not isinstance(item, list) or not all(
                isinstance(child, str) and child.strip() for child in item
            ):
                raise KnowledgeContractError(f"persona {name} must be a text list")
            result = tuple(normalize_text(child) for child in item)
            if required and not result:
                raise KnowledgeContractError(f"persona {name} must not be empty")
            return result

        return cls(
            persona_id=text("persona_id"),
            version=text("version"),
            role=text("role"),
            objectives=texts("objectives"),
            constraints=texts("constraints"),
            allowed_question_types=texts("allowed_question_types"),
            tone=text("tone"),
            forbidden_behaviors=texts("forbidden_behaviors"),
        )._validated()

    def _validated(self) -> "PersonaTemplate":
        unknown = set(self.allowed_question_types) - ALLOWED_QUESTION_TYPES
        if unknown:
            raise KnowledgeContractError("persona contains an unknown question type")
        if len(set(self.allowed_question_types)) != len(self.allowed_question_types):
            raise KnowledgeContractError("persona question types must be unique")
        if set(self.constraints) & set(self.forbidden_behaviors):
            raise KnowledgeContractError(
                "persona constraints conflict with forbidden behaviors"
            )
        return self

    def payload(self) -> dict[str, Any]:
        return {
            "schema_version": "persona_template.v1",
            "persona_id": self.persona_id,
            "version": self.version,
            "role": self.role,
            "objectives": list(self.objectives),
            "constraints": list(self.constraints),
            "allowed_question_types": list(self.allowed_question_types),
            "tone": self.tone,
            "forbidden_behaviors": list(self.forbidden_behaviors),
        }

    @property
    def snapshot_id(self) -> str:
        return digest_text(canonical_json(self.payload()))


@dataclass(frozen=True)
class RetrievedChunk:
    chunk: KnowledgeChunk
    score: float

    def __post_init__(self) -> None:
        if isinstance(self.score, bool) or not isinstance(self.score, (int, float)):
            raise KnowledgeContractError("retrieval score must be numeric")
        if not math.isfinite(float(self.score)) or self.score <= 0:
            raise KnowledgeContractError("retrieval score must be finite and positive")
        object.__setattr__(self, "score", round(float(self.score), 8))
