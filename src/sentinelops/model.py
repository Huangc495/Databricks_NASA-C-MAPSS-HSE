"""Causal features and an engine-disjoint validation protocol."""
import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingRegressor
from sklearn.metrics import mean_absolute_error, mean_squared_error
from sklearn.model_selection import GroupShuffleSplit

SENSORS = [f"sensor_{i}" for i in (2, 3, 4, 7, 8, 9, 11, 12, 13, 14, 15, 17, 20, 21)]
LABEL_CAP = 125  # Also applied by pipelines/medallion.py to Gold training labels.
# Column order of features(); the Gold feature table and model signature share it.
FEATURES = ["cycle", *SENSORS] + [c for s in SENSORS for c in (f"{s}_mean_10", f"{s}_delta_5")]


def features(frame: pd.DataFrame) -> pd.DataFrame:
    frame = frame.sort_values(["unit", "cycle"])
    out = frame[["cycle", *SENSORS]].copy()
    for sensor in SENSORS:
        grouped = frame.groupby("unit")[sensor]
        out[f"{sensor}_mean_10"] = grouped.transform(lambda x: x.rolling(10, min_periods=1).mean())
        out[f"{sensor}_delta_5"] = grouped.diff(5).fillna(0)
    return out.sort_index()


def labels(frame: pd.DataFrame, cap: int = LABEL_CAP) -> pd.Series:
    return (frame.groupby("unit")["cycle"].transform("max") - frame["cycle"]).clip(upper=cap)


def metrics(truth, prediction) -> dict:
    error = np.asarray(prediction) - np.asarray(truth)
    return {
        "rmse": float(np.sqrt(mean_squared_error(truth, prediction))),
        "mae": float(mean_absolute_error(truth, prediction)),
        "nasa_score": float(np.where(error < 0, np.expm1(-error / 13), np.expm1(error / 10)).sum()),
    }


def fit(train: pd.DataFrame):
    return select(features(train), labels(train), train.unit)


def select(x: pd.DataFrame, y: pd.Series, units: pd.Series):
    """Choose leaves on held-out engines, then refit on all engines. Rows are positional."""
    units = np.asarray(units)
    fit_idx, validation_idx = next(GroupShuffleSplit(n_splits=1, test_size=0.2, random_state=42).split(x, y, units))
    results = []
    for leaves in (7, 15, 31):
        candidate = HistGradientBoostingRegressor(max_leaf_nodes=leaves, max_iter=200, learning_rate=0.05, l2_regularization=1, early_stopping=False, random_state=42)
        candidate.fit(x.iloc[fit_idx], y.iloc[fit_idx])
        results.append((metrics(y.iloc[validation_idx], candidate.predict(x.iloc[validation_idx]))["rmse"], leaves))
    validation_rmse, leaves = min(results)
    model = HistGradientBoostingRegressor(max_leaf_nodes=leaves, max_iter=200, learning_rate=0.05, l2_regularization=1, early_stopping=False, random_state=42).fit(x, y)
    return model, {"validation_rmse": validation_rmse, "max_leaf_nodes": leaves, "fit_units": np.unique(units[fit_idx]).tolist(), "validation_units": np.unique(units[validation_idx]).tolist()}
