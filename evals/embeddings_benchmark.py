"""Embedding model mini-benchmark for the memory feature (plan item 4.4, discussion #4).

Not part of pytest/CI: downloads models and runs local inference. Requires the
``embeddings`` dependency group:

    uv run --group embeddings python evals/embeddings_benchmark.py
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import numpy as np

DATA_PATH = Path(__file__).parent / "data" / "memory_bench_ru.json"

MODELS = {
    "bge-m3": {"name": "BAAI/bge-m3", "query_prefix": "", "passage_prefix": ""},
    "multilingual-e5-large": {
        "name": "intfloat/multilingual-e5-large",
        "query_prefix": "query: ",
        "passage_prefix": "passage: ",
    },
}


def evaluate(model_key: str) -> dict[str, float]:
    """Return recall@1, recall@5 and MRR for one model over the dataset."""

    from sentence_transformers import SentenceTransformer

    spec = MODELS[model_key]
    data = json.loads(DATA_PATH.read_text(encoding="utf-8"))
    pairs = data["pairs"]
    facts = [pair["fact"] for pair in pairs] + data["distractors"]

    started = time.monotonic()
    model = SentenceTransformer(spec["name"])
    load_seconds = time.monotonic() - started

    passage_texts = [spec["passage_prefix"] + fact for fact in facts]
    query_texts = [spec["query_prefix"] + pair["query"] for pair in pairs]
    started = time.monotonic()
    passage_vectors = model.encode(passage_texts, normalize_embeddings=True)
    query_vectors = model.encode(query_texts, normalize_embeddings=True)
    encode_seconds = time.monotonic() - started

    similarity = np.asarray(query_vectors) @ np.asarray(passage_vectors).T
    ranks = []
    for index in range(len(pairs)):
        order = np.argsort(-similarity[index])
        ranks.append(int(np.where(order == index)[0][0]) + 1)

    return {
        "recall@1": sum(rank == 1 for rank in ranks) / len(ranks),
        "recall@5": sum(rank <= 5 for rank in ranks) / len(ranks),
        "mrr": float(np.mean([1.0 / rank for rank in ranks])),
        "worst_rank": max(ranks),
        "load_s": round(load_seconds, 1),
        "encode_s": round(encode_seconds, 1),
    }


def main() -> None:
    results = {}
    for model_key in MODELS:
        print(f"evaluating {model_key} ...", flush=True)
        results[model_key] = evaluate(model_key)
        print(model_key, results[model_key], flush=True)

    def sort_key(item: tuple[str, dict[str, float]]) -> tuple[float, float]:
        return (item[1]["recall@5"], item[1]["mrr"])

    winner = max(results.items(), key=sort_key)
    print("\nWINNER:", winner[0], winner[1])


if __name__ == "__main__":
    main()
