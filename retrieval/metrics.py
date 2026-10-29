"""Graded metrics over unique, pre-labelled evidence groups."""

import math


def retrieval_metrics(keys: list[str], qrels: dict[str, int], k: int) -> dict:
    if len(keys) != len(set(keys)):
        raise ValueError("duplicate evidence groups would inflate retrieval metrics")
    if any(type(grade) is not int or not 0 <= grade <= 3 for grade in qrels.values()):
        raise ValueError("relevance grades must be integers from 0 to 3")
    relevant = {key for key, grade in qrels.items() if grade > 0}
    if not relevant:
        return {"recall": None, "ndcg": None, "reason": "no_positive_labels"}
    retrieved = keys[:k]
    dcg = sum(
        (2 ** qrels.get(key, 0) - 1) / math.log2(rank + 1) for rank, key in enumerate(retrieved, 1)
    )
    ideal = sum(
        (2**grade - 1) / math.log2(rank + 1)
        for rank, grade in enumerate(sorted(qrels.values(), reverse=True)[:k], 1)
    )
    return {
        "recall": len(set(retrieved) & relevant) / len(relevant),
        "ndcg": dcg / ideal,
        "reason": None,
    }
