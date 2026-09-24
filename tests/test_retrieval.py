import numpy as np
import pandas as pd
import pytest

from sentinelops.retrieval import ExactIndex, KeywordIndex, truncate
from sentinelops.retrieval_eval import (QUESTIONS, OFF_TOPIC, RULE_COLUMNS, fingerprint, ranking_metrics,
                                        relevance, relevant, score, summarize)


def unit_rows(rows, dims, seed=0):
    matrix = np.random.default_rng(seed).normal(size=(rows, dims)).astype(np.float32)
    return matrix / np.linalg.norm(matrix, axis=1, keepdims=True)


def test_exact_index_matches_brute_force_ranking():
    matrix, queries = unit_rows(500, 32), unit_rows(3, 32, seed=1)
    ids = np.arange(1000, 1500)
    found, scores = ExactIndex(ids, matrix).search(queries, k=7)
    expected = np.argsort(-(queries @ matrix.T), axis=1)[:, :7]
    np.testing.assert_array_equal(found, ids[expected])
    assert (np.diff(scores, axis=1) <= 0).all()


def test_truncation_is_unit_length_prefix():
    matrix = unit_rows(10, 16)
    short = truncate(matrix, 4)
    np.testing.assert_allclose(np.linalg.norm(short, axis=1), 1, atol=1e-6)
    np.testing.assert_allclose(short * np.linalg.norm(matrix[:, :4], axis=1, keepdims=True), matrix[:, :4], atol=1e-6)
    assert ExactIndex(np.arange(10), short).dimensions == 4


def test_index_rejects_duplicates_non_unit_rows_and_wrong_query_size():
    with pytest.raises(ValueError, match="unique"):
        ExactIndex([1, 1], unit_rows(2, 4))
    with pytest.raises(ValueError, match="unit"):
        ExactIndex([1, 2], unit_rows(2, 4) * 2)
    with pytest.raises(ValueError, match="dimensions"):
        ExactIndex([1, 2], unit_rows(2, 4)).search(unit_rows(1, 3))


def test_keyword_baseline_ranks_lexical_match_first():
    texts = ["ladder fall fracture wrist", "forklift ran over foot", "heat exhaustion outdoors",
             "ladder rung broke", "forklift tipped over"]
    ids, _ = KeywordIndex([10, 20, 30, 40, 50], texts).search(["worker run over by a forklift"], k=2)
    assert set(ids[0]) == {20, 50}


def test_ranking_metrics_known_values():
    metrics = ranking_metrics([9, 1, 8, 2, 7, 6, 5, 4, 3, 0], {1, 2}, k=10)
    assert metrics["p_at_5"] == pytest.approx(0.4) and metrics["p_at_10"] == pytest.approx(0.2)
    assert metrics["mrr"] == 0.5 and metrics["hit_at_1"] == 0
    ideal = 1 + 1 / np.log2(3)
    assert metrics["ndcg_at_10"] == pytest.approx((1 / np.log2(3) + 1 / np.log2(5)) / ideal)
    with pytest.raises(ValueError):
        ranking_metrics([1, 2], {1}, k=10)


def test_rules_are_and_of_clauses_with_or_across_columns():
    frame = pd.DataFrame({"report_id": [1, 2, 3], "nature_title": ["Amputations", "Fractures", "Amputations"],
                          "source_title": ["Brake presses", None, "Table saws"],
                          "secondary_source_title": [None, "Power presses", None]})
    rule = [{"nature_title": "amput"}, {"source_title": r"\bpress", "secondary_source_title": r"\bpress"}]
    assert relevant(frame, rule).tolist() == [True, False, False]
    assert relevant(frame, [{"source_title": r"\bpress", "secondary_source_title": r"\bpress"}]).tolist() == [True, True, False]
    with pytest.raises(ValueError):
        relevant(frame, [{"narrative": "x"}])
    truth = relevance(frame, [{"id": "q", "rule": rule}])
    rows = score("m", [[3, 1, 2]], [[0.9, 0.8, 0.1]], truth, [{"id": "q", "rule": rule}], k=3)
    assert rows[0]["mrr"] == 0.5 and rows[0]["lift_at_10"] == pytest.approx((1 / 3) / (1 / 3))
    assert summarize(pd.DataFrame(rows)).loc["m", "top1_score"] == pytest.approx(0.9)


def test_evaluation_set_is_well_formed():
    ids = [q["id"] for q in QUESTIONS + OFF_TOPIC]
    assert len(ids) == len(set(ids)) and len(QUESTIONS) >= 25 and len(OFF_TOPIC) >= 3
    for question in QUESTIONS:
        assert question["rule"] and all(clause and set(clause) <= RULE_COLUMNS for clause in question["rule"])
    assert fingerprint() == fingerprint() and len(fingerprint()) == 12
