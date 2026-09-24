from dataclasses import replace

import pandas as pd
import pytest
from sentinelops.medallion import digest, label_frame, training_frame
from sentinelops.model import FEATURES
from sentinelops.promotion import Evidence, constant_baseline_rmse, decide, retrain_decision
from test_medallion import gold, trajectories

CHALLENGER = Evidence(version=3, subset="FD001", training_source="gold_medallion", input_columns=tuple(FEATURES),
                      validation_rmse=15.0, fit_units=(1, 2, 3), validation_units=(4, 5), labels_digest="abc")


def test_first_promotion_needs_lineage_contract_and_baseline_margin():
    assert decide(CHALLENGER, None, baseline_rmse=40.0) == []
    assert "Gold" in decide(replace(CHALLENGER, training_source=None), None, 40.0)[0]
    assert "signature" in decide(replace(CHALLENGER, input_columns=("cycle",)), None, 40.0)[0]
    assert "baseline" in decide(CHALLENGER, None, baseline_rmse=25.0)[0]
    assert "baseline" in decide(CHALLENGER, None, baseline_rmse=None)[0]


def test_challenger_must_match_or_beat_a_comparable_champion():
    champion = replace(CHALLENGER, version=2, validation_rmse=14.0)
    assert "worse" in decide(CHALLENGER, champion, 40.0)[0]
    assert decide(CHALLENGER, champion, 40.0, tolerance=1.0) == []
    assert decide(CHALLENGER, replace(champion, validation_rmse=15.0), 40.0) == []
    assert "comparable" in decide(CHALLENGER, replace(champion, validation_units=(6, 7)), 40.0)[0]
    assert "comparable" in decide(CHALLENGER, replace(champion, labels_digest="xyz"), 40.0)[0]
    assert "already" in decide(CHALLENGER, CHALLENGER, 40.0)[0]


def test_baseline_uses_fit_mean_on_validation_engines_only():
    labels = pd.DataFrame({"unit": [1, 1, 2, 3], "rul": [10, 20, 30, 50]})
    assert constant_baseline_rmse(labels, [1, 2], [3]) == pytest.approx(30.0)
    with pytest.raises(ValueError):
        constant_baseline_rmse(labels, [1], [9])


def test_label_frame_digest_matches_training_digest():
    x, y = gold(trajectories())
    x, y = x.astype({"unit": "int32"}), y.astype({"unit": "int32"})
    train = training_frame(x, y, "FD001")
    columns = ["unit", "cycle", "rul"]
    assert digest(label_frame(y, "FD001")[columns]) == digest(train[columns])


def test_retraining_happens_only_when_gold_training_inputs_change():
    current = {"features": "f1", "training_labels": "l1", "test_endpoints": "t1"}
    champion = {"digest_features": "f1", "digest_training_labels": "l1", "digest_test_endpoints": "t0"}
    assert retrain_decision(current, champion) == (False, "Gold training inputs unchanged")  # Test labels don't count.
    assert retrain_decision(current, {**champion, "digest_training_labels": "l0"}) == (True, "changed: training_labels")
    assert retrain_decision(current, {}) == (True, "changed: features, training_labels")
    assert retrain_decision(current, None) == (True, "no champion")
    assert retrain_decision(current, champion, force=True) == (True, "forced")
