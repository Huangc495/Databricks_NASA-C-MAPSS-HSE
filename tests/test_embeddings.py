import numpy as np
import pytest

from sentinelops.embeddings import embed, format_query, unit_vectors


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def fake_call(dimensions=4, fail_first=0, always_fail=()):
    calls = {"n": 0}

    def call(batch):
        calls["n"] += 1
        if calls["n"] <= fail_first or any(text in always_fail for text in batch):
            raise RuntimeError("429 Too Many Requests")
        vectors = np.zeros((len(batch), dimensions))
        for i, text in enumerate(batch):
            vectors[i, int(text) % dimensions] = 1.0  # Deterministic unit vector per text.
        return vectors, len(batch) * 3
    return call, calls


def run(texts, call, **kwargs):
    clock = FakeClock()
    return (*embed(texts, call, dimensions=4, clock=clock, sleep=clock.sleep, **kwargs), clock)


def test_embed_preserves_order_counts_tokens_and_paces_requests():
    texts = [str(i) for i in range(10)]
    call, _ = fake_call()
    matrix, ok, stats, clock = run(texts, call, batch_size=3, min_interval=0.5)
    assert ok.all() and stats["tokens"] == 30 and stats["requests"] == 4
    assert [int(np.argmax(row)) for row in matrix] == [i % 4 for i in range(10)]
    assert clock.now == pytest.approx(1.5)  # Four requests, 0.5 s apart.


def test_throttling_slows_the_pace_and_retries():
    texts = [str(i) for i in range(6)]
    call, calls = fake_call(fail_first=2)
    _, ok, stats, _ = run(texts, call, batch_size=6, retries=3, min_interval=0.5, backoff_seconds=0)
    assert ok.all() and calls["n"] == 3 and stats["failed_requests"] == 2
    assert stats["final_interval"] == pytest.approx(1.125) and "429" in stats["last_errors"][-1]


def test_persistent_failures_are_isolated_to_their_batch():
    texts = [str(i) for i in range(6)]
    call, _ = fake_call(always_fail={"4"})
    matrix, ok, stats, _ = run(texts, call, batch_size=2, retries=1, backoff_seconds=0)
    assert ok.tolist() == [True, True, True, True, False, False]
    assert np.isnan(matrix[4:]).all() and stats["tokens"] == 12 and stats["failed_requests"] == 2


def test_unit_vectors_rejects_wrong_size_nan_and_unnormalized():
    assert unit_vectors([[0.6, 0.8]], dimensions=2).dtype == np.float32
    for bad in ([[1.0, 0.0, 0.0]], [[np.nan, 1.0]], [[2.0, 0.0]]):
        with pytest.raises(ValueError):
            unit_vectors(bad, dimensions=2)


def test_queries_carry_the_instruction_documents_do_not():
    assert format_query("press brake amputations").startswith("Instruct: ")
    assert format_query("x").endswith("\nQuery:x")
