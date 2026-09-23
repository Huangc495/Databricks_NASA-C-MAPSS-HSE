"""Validation-based champion/challenger gate. Official test labels never enter it."""
from dataclasses import dataclass

import numpy as np
import pandas as pd

from sentinelops.model import FEATURES, metrics

GOLD_SOURCE = "gold_medallion"


@dataclass(frozen=True)
class Evidence:
    """What a registered version's training run recorded."""
    version: int
    subset: str | None
    training_source: str | None
    input_columns: tuple[str, ...]
    validation_rmse: float
    fit_units: tuple[int, ...]
    validation_units: tuple[int, ...]
    labels_digest: str | None


def constant_baseline_rmse(labels: pd.DataFrame, fit_units, validation_units) -> float:
    """RMSE on validation engines of predicting the fit engines' mean label."""
    fit = labels[labels.unit.isin(fit_units)]
    validation = labels[labels.unit.isin(validation_units)]
    if fit.empty or validation.empty:
        raise ValueError("Validation baseline needs labels for both fit and validation engines")
    return metrics(validation.rul, np.repeat(fit.rul.mean(), len(validation)))["rmse"]


def decide(challenger: Evidence, champion: Evidence | None, baseline_rmse: float | None,
           max_baseline_ratio: float = 0.5, tolerance: float = 0.0) -> list[str]:
    """Blocking reasons; an empty list means promote the challenger."""
    reasons = []
    if challenger.training_source != GOLD_SOURCE:
        reasons.append("challenger was not trained from the Gold medallion features")
    if challenger.input_columns != tuple(FEATURES):
        reasons.append("challenger signature does not match the Gold feature contract")
    if baseline_rmse is None or not challenger.validation_rmse <= max_baseline_ratio * baseline_rmse:
        reasons.append(f"validation RMSE {challenger.validation_rmse:.4f} is not at most "
                       f"{max_baseline_ratio} x the constant baseline ({baseline_rmse})")
    if champion is not None:
        if champion.version == challenger.version:
            reasons.append("challenger is already the champion")
        elif (champion.subset, champion.validation_units, champion.labels_digest) != (
                challenger.subset, challenger.validation_units, challenger.labels_digest):
            reasons.append("not comparable with the champion: different subset, validation engines or labels")
        elif not challenger.validation_rmse <= champion.validation_rmse + tolerance:
            reasons.append(f"validation RMSE {challenger.validation_rmse:.4f} is worse than champion "
                           f"v{champion.version} ({champion.validation_rmse:.4f}) beyond tolerance {tolerance}")
    return reasons
