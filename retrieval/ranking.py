"""Transparent BM25 and rank fusion; similarity is never an applicability assertion."""

import math
import re
from collections import Counter


def tokenize(text: str) -> list[str]:
    text = re.sub(r"\b(\d{2})v-(\d{3})\b", lambda m: f"{m[1]}v{m[2]}000", text.casefold())
    return re.findall(r"[^\W_]+(?:-[^\W_]+)*", text)


def build_bm25(texts: list[str]) -> dict:
    postings: dict[str, list[list[int]]] = {}
    lengths = []
    for index, text in enumerate(texts):
        terms = Counter(tokenize(text))
        lengths.append(sum(terms.values()))
        for term, count in terms.items():
            postings.setdefault(term, []).append([index, count])
    return {
        "postings": postings,
        "lengths": lengths,
        "average_length": sum(lengths) / len(lengths) if lengths else 0,
    }


def bm25_scores(index: dict, query: str, k1: float, b: float) -> dict[int, float]:
    scores: dict[int, float] = {}
    n = len(index["lengths"])
    if not n or not index["average_length"]:
        return scores
    for token in sorted(set(tokenize(query))):
        entries = index["postings"].get(token, [])
        idf = math.log(1 + (n - len(entries) + 0.5) / (len(entries) + 0.5))
        for position, frequency in entries:
            norm = k1 * (1 - b + b * index["lengths"][position] / index["average_length"])
            scores[position] = scores.get(position, 0) + idf * frequency * (k1 + 1) / (
                frequency + norm
            )
    return scores


def ranked(
    scores: dict[int, float], eligible: set[int], identifiers: list[str], limit: int
) -> list[tuple[int, float]]:
    return sorted(
        ((i, s) for i, s in scores.items() if i in eligible),
        key=lambda item: (-item[1], identifiers[item[0]]),
    )[:limit]


def rrf(rankings: list[list[tuple[int, float]]], constant: int) -> dict[int, float]:
    scores: dict[int, float] = {}
    for ranking in rankings:
        for position, (identifier, _) in enumerate(ranking, 1):
            scores[identifier] = scores.get(identifier, 0) + 1 / (constant + position)
    return scores
