import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from sentinelops import serving
from sentinelops.model import FEATURES

ROOT = Path(__file__).resolve().parents[1]
KEYS = ["dataset", "subset", "split", "unit", "cycle"]


def rows(n=3, seed=0):
    values = np.random.default_rng(seed).normal(size=(n, len(FEATURES)))
    frame = pd.DataFrame(values, columns=FEATURES)
    return frame.assign(cycle=np.arange(1, n + 1, dtype="int32"))


def test_request_body_round_trips_doubles_exactly_and_keeps_cycle_integral():
    frame = rows()
    frame.loc[0, "sensor_2_mean_10"] = 0.1 + 0.2  # not representable as a short decimal
    frame.loc[1, "sensor_2_mean_10"] = 642.5 + 3.6e-12  # the size of the Spark/pandas mean difference
    body = json.loads(serving.request_body(frame))["dataframe_split"]
    assert body["columns"] == FEATURES
    decoded = pd.DataFrame(body["data"], columns=body["columns"])
    assert (decoded[FEATURES].to_numpy() == frame[FEATURES].to_numpy()).all()
    assert all(isinstance(row[0], int) for row in body["data"])  # cycle is the first feature


def test_request_body_rejects_duplicate_columns_that_would_shift_values():
    # cycle is both a Gold key and a feature: selecting keys + features repeats it (run 959774648252926).
    frame = rows()
    doubled = pd.concat([frame[["cycle"]], frame], axis=1)
    with pytest.raises(ValueError, match="Duplicate columns: \\['cycle'\\]"):
        serving.request_body(doubled)


def test_request_body_rejects_missing_or_non_finite_values():
    frame = rows()
    frame.loc[0, "sensor_3"] = np.nan
    with pytest.raises(ValueError, match="nulls"):
        serving.request_body(frame)
    frame.loc[0, "sensor_3"] = np.inf
    with pytest.raises(ValueError, match="finite"):
        serving.request_body(frame)


def test_predictions_must_match_the_row_count():
    assert serving.predictions('{"predictions": [1.5, 2]}', 2) == [1.5, 2.0]
    with pytest.raises(ValueError, match="1 predictions for 2 rows"):
        serving.predictions('{"predictions": [1.5]}', 2)


class FakeSession:
    def __init__(self, statuses):
        self.statuses, self.calls = list(statuses), 0

    def post(self, url, headers, data, timeout):
        self.calls += 1
        status = self.statuses.pop(0)
        return type("Response", (), {"status_code": status, "text": '{"predictions": [1.0]}' if status == 200 else "no"})


def test_post_retries_cold_starts_visibly_and_fails_on_client_errors():
    session = FakeSession([503, 504, 200])
    text, stats = serving.post(session, "u", {}, "{}", sleep=lambda s: None)
    assert text == '{"predictions": [1.0]}' and stats["retried"] == [503, 504] and session.calls == 3
    with pytest.raises(RuntimeError, match="HTTP 400 after 1"):
        serving.post(FakeSession([400]), "u", {}, "{}", sleep=lambda s: None)
    with pytest.raises(RuntimeError, match="HTTP 503 after 2"):
        serving.post(FakeSession([503, 503]), "u", {}, "{}", attempts=2, sleep=lambda s: None)


def test_parity_counts_bit_identical_rows_and_unmatched_keys():
    keys = pd.DataFrame([("cmapss", "FD001", "test", 1, c) for c in (1, 2, 3)], columns=KEYS)
    served = keys.assign(predicted_rul=[10.0, 20.0, 30.0 + 1e-12])
    logged = keys.assign(predicted_rul=[10.0, 20.0, 30.0])
    result = serving.parity(served, logged, KEYS)
    assert result["rows_compared"] == 3 and result["bit_identical"] == 2 and result["rows_unmatched"] == 0
    assert result["mismatches_over_1e_9"] == 0 and 0 < result["max_abs_diff"] < 1e-11
    assert serving.parity(served.iloc[:2], logged, KEYS)["rows_unmatched"] == 1


def test_endpoint_definition_is_a_bounded_scale_to_zero_cpu_demo_with_an_inference_table():
    spec = json.loads((ROOT / "infra" / "serving-endpoint.json").read_text())
    assert not spec["name"].startswith("databricks-")
    (entity,) = spec["config"]["served_entities"]
    assert entity["scale_to_zero_enabled"] is True and entity["workload_size"] == "Small"
    assert entity["workload_type"] == "CPU" and entity["entity_version"].isdigit()
    assert spec["ai_gateway"]["inference_table_config"]["enabled"] is True
    assert {"key": "lifetime", "value": "bounded-demo"} in spec["tags"]
