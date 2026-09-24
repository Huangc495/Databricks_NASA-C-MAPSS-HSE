"""Delayed-label performance and feature drift for the fleet inference log (pandas side)."""
import numpy as np
import pandas as pd

from sentinelops.model import LABEL_CAP, metrics

# True-RUL ranges; the model's labels were capped at LABEL_CAP, so the last range is
# expected to be underestimated by design.
BUCKETS = {"rul_000_024": (0, 25), "rul_025_049": (25, 50), "rul_050_074": (50, 75),
           "rul_075_125": (75, LABEL_CAP + 1), "rul_126_plus": (LABEL_CAP + 1, np.inf)}


def segments(frame: pd.DataFrame):
    yield "all", frame
    yield "endpoint", frame[frame.is_endpoint]
    yield f"actual_le_{LABEL_CAP}", frame[frame.actual_rul <= LABEL_CAP]
    for name, (low, high) in BUCKETS.items():
        yield name, frame[(frame.actual_rul >= low) & (frame.actual_rul < high)]


def performance(frame: pd.DataFrame) -> pd.DataFrame:
    """Per model version and segment: rows, RMSE, MAE, bias (+ = overestimate), NASA score."""
    rows = []
    for version, labelled in frame.groupby("model_version", sort=True):
        for segment, part in segments(labelled):
            if part.empty:
                continue
            result = metrics(part.actual_rul, part.predicted_rul)
            rows.append({"model_version": int(version), "segment": segment, "rows": len(part), **result,
                         "bias": float((part.predicted_rul - part.actual_rul).mean())})
    return pd.DataFrame(rows, columns=["model_version", "segment", "rows", "rmse", "mae", "nasa_score", "bias"])


def _quantile_edges(values, bins: int) -> np.ndarray:
    return np.unique(np.quantile(values, np.linspace(0, 1, bins + 1)[1:-1]))


def _shares(values, edges, weights=None) -> np.ndarray:
    counts = np.bincount(np.searchsorted(edges, values, side="right"), weights=weights, minlength=len(edges) + 1)
    return np.clip(counts / counts.sum(), 1e-6, None)


def psi(reference: pd.Series, current: pd.Series, bins: int = 10, weights=None) -> float:
    """Population stability index over reference-quantile bins (0 = identical)."""
    edges = _quantile_edges(reference, bins)
    expected, actual = _shares(reference, edges, weights), _shares(current, edges)
    return float(np.sum((actual - expected) * np.log(actual / expected)))


def age_weights(reference_age: pd.Series, current_age: pd.Series, bins: int = 10) -> np.ndarray:
    """Reweight reference rows so their age (cycle) mix matches the current rows.
    Reference rows younger or older than every current row get zero weight."""
    edges = _quantile_edges(current_age, bins)
    reference_age, current_age = np.asarray(reference_age), np.asarray(current_age)
    in_range = (reference_age >= current_age.min()) & (reference_age <= current_age.max())
    reference_bin = np.searchsorted(edges, reference_age, side="right")
    reference_share = np.bincount(reference_bin[in_range], minlength=len(edges) + 1) / max(in_range.sum(), 1)
    current_share = np.bincount(np.searchsorted(edges, current_age, side="right"), minlength=len(edges) + 1) / len(current_age)
    if (reference_share[current_share > 0] == 0).any():
        raise ValueError("Current ages outside the reference range cannot be matched")
    ratio = current_share / np.where(reference_share > 0, reference_share, 1)
    return np.where(in_range, ratio[reference_bin], 0.0)


ALERT_SEGMENTS = ("endpoint", f"actual_le_{LABEL_CAP}")


def alert_checks(performance: pd.DataFrame, drift_snapshots: pd.DataFrame, now: pd.Timestamp,
                 max_rmse_increase: float, max_psi: float, max_age_hours: float) -> pd.DataFrame:
    """One row per check (check, subject, value, threshold, breached) for one model version.

    `performance` holds that version's snapshots (segment, rmse, computed_at). RMSE is
    compared with the version's first snapshot, a relative threshold fixed in advance, never
    with test-set results. `drift_snapshots` (feature, psi_age_matched, computed_at) are
    checked at their latest snapshot. Snapshots older than `max_age_hours` also breach.
    """
    rows = []
    for name, frame in (("performance", performance), ("drift", drift_snapshots)):
        age = (now - frame.computed_at.max()).total_seconds() / 3600 if len(frame) else float("inf")
        rows.append({"check": "snapshot_age_hours", "subject": name, "value": age, "threshold": max_age_hours,
                     "breached": bool(age > max_age_hours)})
    for segment in ALERT_SEGMENTS:
        history = performance[performance.segment == segment].sort_values("computed_at")
        if history.empty:
            rows.append({"check": "rmse_increase", "subject": segment, "value": float("nan"),
                         "threshold": max_rmse_increase, "breached": True})
            continue
        increase = history.rmse.iloc[-1] / history.rmse.iloc[0] - 1
        rows.append({"check": "rmse_increase", "subject": segment, "value": float(increase),
                     "threshold": max_rmse_increase, "breached": bool(increase > max_rmse_increase)})
    if len(drift_snapshots):
        latest = drift_snapshots[drift_snapshots.computed_at == drift_snapshots.computed_at.max()]
        rows += [{"check": "psi_age_matched", "subject": row.feature, "value": float(row.psi_age_matched),
                  "threshold": max_psi, "breached": bool(row.psi_age_matched > max_psi)}
                 for row in latest.itertuples()]
    return pd.DataFrame(rows, columns=["check", "subject", "value", "threshold", "breached"])


def drift(reference: pd.DataFrame, current: pd.DataFrame, columns: list[str], age: str = "cycle") -> pd.DataFrame:
    """Raw PSI, plus PSI against an age-matched reference. Fleet engines are observed
    earlier in life than run-to-failure training data, so raw PSI mostly measures age."""
    if reference.empty or current.empty:
        raise ValueError("Drift needs reference and current rows")
    weights = age_weights(reference[age], current[age])
    return pd.DataFrame([{"feature": c, "psi": psi(reference[c], current[c]),
                          "psi_age_matched": psi(reference[c], current[c], weights=weights),
                          "reference_mean": float(reference[c].mean()), "current_mean": float(current[c].mean()),
                          "reference_rows": len(reference), "current_rows": len(current)} for c in columns])
