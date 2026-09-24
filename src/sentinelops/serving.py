"""Real-time scoring client for the RUL serving endpoint, and the batch-parity check.

The endpoint serves a champion version on Gold-format feature rows: the Spark-computed columns
of gold.cmapss_features, in sentinelops.model.FEATURES order. Features are never recomputed
with pandas (Spark and pandas rolling means differ by ~1e-12, which moves gradient-boosting
predictions). JSON floats round-trip doubles exactly, so served predictions can be compared with
the batch inference log bit for bit.
"""
import json
import math
import time

import numpy as np
import pandas as pd

from sentinelops.model import FEATURES

# Responses up to 1 MiB are logged to the inference table; 400 rows x 43 features is ~300 KB.
BATCH_ROWS = 400
RETRY_STATUS = (429, 502, 503, 504)  # cold start from scale-to-zero, throttling


def request_body(rows: pd.DataFrame) -> str:
    """MLflow `dataframe_split` JSON for Gold feature rows, with native ints and exact doubles."""
    if rows.columns.duplicated().any():  # a repeated label would shift every value after it
        raise ValueError(f"Duplicate columns: {sorted(set(rows.columns[rows.columns.duplicated()]))}")
    frame = rows[FEATURES]
    if frame.isna().any().any():
        raise ValueError("Gold feature rows must not contain nulls")
    data = [[int(value) if column == "cycle" else float(value) for column, value in zip(FEATURES, row)]
            for row in frame.itertuples(index=False, name=None)]
    if not all(math.isfinite(v) for row in data for v in row):
        raise ValueError("Gold feature rows must be finite")
    return json.dumps({"dataframe_split": {"columns": FEATURES, "data": data}})


def predictions(response_text: str, expected: int) -> list[float]:
    """Parse {"predictions": [...]} and check the count."""
    values = json.loads(response_text)["predictions"]
    if len(values) != expected:
        raise ValueError(f"Endpoint returned {len(values)} predictions for {expected} rows")
    return [float(v) for v in values]


def post(session, url: str, headers: dict, body: str, attempts: int = 8, backoff: float = 15.0,
         sleep=time.sleep) -> tuple[str, dict]:
    """POST with visible retries on cold start and throttling; returns (text, stats).

    Every retried status is recorded, so a slow first call is visible rather than silent.
    """
    retried = []
    for attempt in range(1, attempts + 1):
        started = time.monotonic()
        response = session.post(url, headers=headers, data=body, timeout=120)
        seconds = time.monotonic() - started
        if response.status_code == 200:
            return response.text, {"seconds": seconds, "retried": retried}
        if response.status_code not in RETRY_STATUS or attempt == attempts:
            raise RuntimeError(f"HTTP {response.status_code} after {attempt} attempt(s): {response.text[:300]}")
        retried.append(response.status_code)
        sleep(backoff * attempt)
    raise AssertionError("unreachable")


def parity(served: pd.DataFrame, logged: pd.DataFrame, keys: list[str]) -> dict:
    """Compare served predictions with the batch inference log for the same version and rows."""
    joined = served.merge(logged, on=keys, how="outer", suffixes=("_served", "_logged"), indicator=True)
    missing = int((joined["_merge"] != "both").sum())
    both = joined[joined["_merge"] == "both"]
    a, b = both.predicted_rul_served.to_numpy(), both.predicted_rul_logged.to_numpy()
    diff = np.abs(a - b)
    return {"rows_compared": int(len(both)), "rows_unmatched": missing,
            "bit_identical": int((a == b).sum()), "max_abs_diff": float(diff.max()) if len(diff) else None,
            "mismatches_over_1e_9": int((diff > 1e-9).sum())}


def latency_summary(seconds: list[float]) -> dict:
    values = np.asarray(seconds) * 1000
    return {"calls": int(len(values)), "p50_ms": round(float(np.percentile(values, 50)), 1),
            "p95_ms": round(float(np.percentile(values, 95)), 1), "max_ms": round(float(values.max()), 1)}
