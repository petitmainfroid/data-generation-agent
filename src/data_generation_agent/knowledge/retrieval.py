from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass

from .models import KnowledgeContractError, RetrievedChunk, normalize_text, retrieval_tokens
from .store import KnowledgeStore


@dataclass(frozen=True)
class RetrievalQuery:
    text: str
    approved_snapshot_ids: tuple[str, ...]
    top_k: int = 5

    def __post_init__(self) -> None:
        normalized = normalize_text(self.text)
        if not normalized:
            raise KnowledgeContractError("retrieval query must not be empty")
        if not self.approved_snapshot_ids:
            raise KnowledgeContractError("retrieval scope must not be empty")
        if isinstance(self.top_k, bool) or not isinstance(self.top_k, int) or not 1 <= self.top_k <= 20:
            raise KnowledgeContractError("top_k must be between 1 and 20")
        object.__setattr__(self, "text", normalized)
        object.__setattr__(
            self, "approved_snapshot_ids", tuple(sorted(set(self.approved_snapshot_ids)))
        )


class DeterministicRetriever:
    def __init__(self, store: KnowledgeStore) -> None:
        self.store = store

    def retrieve(self, query: RetrievalQuery) -> tuple[RetrievedChunk, ...]:
        chunks = self.store.chunks(query.approved_snapshot_ids)
        query_counts = Counter(retrieval_tokens(query.text))
        if not query_counts:
            return ()
        document_frequency: Counter[str] = Counter()
        for chunk in chunks:
            document_frequency.update(set(chunk.tokens))
        total = max(len(chunks), 1)
        ranked: list[RetrievedChunk] = []
        for chunk in chunks:
            counts = Counter(chunk.tokens)
            score = 0.0
            for token, query_frequency in query_counts.items():
                if token not in counts:
                    continue
                inverse = math.log((total + 1) / (document_frequency[token] + 1)) + 1
                score += min(counts[token], 3) * query_frequency * inverse
            if score > 0:
                ranked.append(RetrievedChunk(chunk=chunk, score=round(score, 8)))
        ranked.sort(key=lambda item: (-item.score, item.chunk.chunk_id))
        return tuple(ranked[: query.top_k])

    def reverse_lookup(self, citation: str):
        return self.store.chunk_by_citation(citation)
