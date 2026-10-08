"""Database-scoped administrator knowledge base (question -> known-good DAX)."""

from __future__ import annotations

from dataclasses import dataclass
import json
import re
from typing import Iterable


@dataclass(frozen=True)
class DaxExample:
    question: str
    dax: str


def _tokens(text: str) -> set[str]:
    return {t.casefold() for t in re.findall(r"\w+", text, flags=re.UNICODE) if len(t) > 1}


class KnowledgeBase:
    """Small, deterministic lexical retriever scoped to one selected database."""

    def __init__(self, examples: Iterable[DaxExample] = ()):
        self.examples = tuple(e for e in examples if e.dax.strip())

    def search(self, query: str, limit: int = 5) -> list[DaxExample]:
        limit = max(1, min(int(limit), 20))
        normalized = " ".join(query.casefold().split())
        qtokens = _tokens(query)
        scored: list[tuple[float, int, DaxExample]] = []
        for index, example in enumerate(self.examples):
            e_norm = " ".join(example.question.casefold().split())
            etokens = _tokens(example.question)
            if normalized and normalized == e_norm:
                score = 1000.0
            else:
                overlap = len(qtokens & etokens)
                union = len(qtokens | etokens) or 1
                score = overlap * 10.0 + overlap / union
                if normalized and (normalized in e_norm or e_norm in normalized):
                    score += 5.0
            if score > 0:
                scored.append((score, -index, example))
        scored.sort(reverse=True, key=lambda item: (item[0], item[1]))
        return [item[2] for item in scored[:limit]]

    def as_tool_result(self, query: str, limit: int = 5) -> dict:
        hits = self.search(query, limit)
        return {
            "matches": [{"question": e.question, "dax": e.dax} for e in hits],
            "match_count": len(hits),
            "note": (
                "These are administrator-supplied reference queries for this database only. "
                "Adapt them to the current request and live model; do not treat them as query results."
            ),
        }

    def prompt_context(self, query: str, limit: int = 3) -> str:
        hits = self.search(query, limit)
        if not hits:
            return "No close administrator-supplied DAX example was found automatically."
        payload = [{"question": e.question, "dax": e.dax} for e in hits]
        return (
            "Relevant administrator-supplied reference DAX for THIS database only. "
            "It is guidance, not live data. Adapt dates/filters/grouping and validate against the live model:\n"
            + json.dumps(payload, ensure_ascii=False, indent=2)
        )
