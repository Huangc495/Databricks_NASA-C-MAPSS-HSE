"""Paced, validated calls to a pay-per-token embedding endpoint (network call injected).

Used for query-time embedding. Bulk document embedding uses ai_query in
jobs/embed_osha.py, which measured ~470 docs/s. Direct REST calls from this workspace
are throttled by *input count*, not tokens: 16 inputs per request are accepted, 32 are
rejected, and ~24 inputs/s is sustainable. So requests here are sequential and
self-pacing: a throttled request slows the pace, and a run of successes restores it.
"""
import time

import numpy as np

ENDPOINT = "databricks-qwen3-embedding-0-6b"
DIMENSIONS = 1024
BATCH_SIZE = 16
# Qwen3-Embedding is instruction-aware: queries carry a task instruction, documents don't.
QUERY_INSTRUCTION = "Given a workplace safety question, retrieve OSHA severe injury reports relevant to it"
DBU_PER_MILLION_TOKENS = 0.286  # Databricks list rate for Qwen3 Embedding 0.6B, checked 2026-09-23.


def format_query(question: str) -> str:
    return f"Instruct: {QUERY_INSTRUCTION}\nQuery:{question}"


def unit_vectors(rows, dimensions: int = DIMENSIONS) -> np.ndarray:
    """Float32 matrix; every row must be finite, the right size and unit length."""
    matrix = np.asarray(rows, dtype=np.float32)
    if matrix.ndim != 2 or matrix.shape[1] != dimensions or not np.isfinite(matrix).all():
        raise ValueError(f"Expected finite {dimensions}-dimensional embeddings, got {matrix.shape}")
    if not np.allclose(np.linalg.norm(matrix, axis=1), 1.0, atol=1e-3):
        raise ValueError("Embeddings are not unit length")
    return matrix


def embed(texts: list[str], call, batch_size: int = BATCH_SIZE, min_interval: float = 0.5,
          max_interval: float = 4.0, retries: int = 6, backoff_seconds: float = 1.0,
          dimensions: int = DIMENSIONS, clock=time.monotonic, sleep=time.sleep):
    """Embed texts in order. `call(batch) -> (vectors, tokens)` performs one request.

    Returns (matrix with NaN rows for batches that still failed after retries, ok mask,
    stats). Failed rows are left for the next incremental run, not failing everything.
    """
    matrix = np.full((len(texts), dimensions), np.nan, dtype=np.float32)
    ok = np.zeros(len(texts), dtype=bool)
    stats = {"tokens": 0, "requests": 0, "failed_requests": 0, "last_errors": []}
    interval, streak, last = min_interval, 0, float("-inf")
    for start in range(0, len(texts), batch_size):
        batch = texts[start:start + batch_size]
        for attempt in range(retries + 1):
            wait = last + interval - clock()
            if wait > 0:
                sleep(wait)
            last = clock()
            stats["requests"] += 1
            try:
                vectors, tokens = call(batch)
                vectors = unit_vectors(vectors, dimensions)
            except Exception as error:  # Throttling, transient 5xx or a malformed response.
                stats["failed_requests"] += 1
                stats["last_errors"] = (stats["last_errors"] + [str(error)[:200]])[-3:]
                interval, streak = min(max_interval, interval * 1.5), 0
                if attempt < retries:
                    sleep(backoff_seconds * 2 ** attempt)
                continue
            matrix[start:start + len(batch)] = vectors
            ok[start:start + len(batch)] = True
            stats["tokens"] += int(tokens)
            streak += 1
            if streak >= 50:
                interval, streak = max(min_interval, interval * 0.8), 0
            break
    stats["final_interval"] = round(interval, 3)
    return matrix, ok, stats


def cost_dbus(tokens: int) -> float:
    return tokens / 1e6 * DBU_PER_MILLION_TOKENS
