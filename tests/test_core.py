import numpy as np
import pandas as pd
import pytest
from sentinelops.data import COLUMNS, read_trajectories
from sentinelops.model import SENSORS, features, labels, metrics


def trajectory():
    return pd.DataFrame({"unit": [1] * 12 + [2] * 12, "cycle": list(range(1, 13)) * 2, **{s: np.arange(24, dtype=float) for s in SENSORS}})


def test_features_do_not_see_future_or_other_engine():
    frame = trajectory()
    expected = features(frame)
    frame.loc[8:, SENSORS] = 999
    pd.testing.assert_frame_equal(expected.iloc[:8], features(frame).iloc[:8])
    assert expected.loc[12, "sensor_2_mean_10"] == 12
    assert expected.loc[12, "sensor_2_delta_5"] == 0


def test_labels_and_asymmetric_penalty():
    y = labels(trajectory(), cap=5)
    assert y.iloc[0] == 5 and y.iloc[11] == 0 and y.iloc[12] == 5
    assert metrics([10], [20])["nasa_score"] > metrics([10], [0])["nasa_score"]


def test_whitespace_and_duplicate_rejection(tmp_path):
    path = tmp_path / "train.txt"
    row = "  ".join(["1", "1"] + ["0"] * 24) + "   \n"
    path.write_text(row)
    assert list(read_trajectories(path).columns) == COLUMNS
    path.write_text(row * 2)
    with pytest.raises(ValueError, match="Duplicate"):
        read_trajectories(path)


def test_bad_schema_rejected(tmp_path):
    path = tmp_path / "bad.txt"
    path.write_text("1 2 3\n")
    with pytest.raises(ValueError, match="26"):
        read_trajectories(path)
