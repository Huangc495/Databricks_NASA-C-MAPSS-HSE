import numpy as np
import pandas as pd
import pytest
from sentinelops.monitoring import drift, performance, psi


def test_performance_segments_and_bias_per_model_version():
    frame = pd.DataFrame({"model_version": [3, 3, 3, 4], "actual_rul": [10, 100, 200, 10],
                          "predicted_rul": [12.0, 90.0, 125.0, 10.0], "is_endpoint": [True, False, False, True]})
    result = performance(frame).set_index(["model_version", "segment"])
    assert result.loc[(3, "all"), "rows"] == 3
    assert result.loc[(3, "endpoint"), "rmse"] == pytest.approx(2.0)
    assert result.loc[(3, "endpoint"), "bias"] == pytest.approx(2.0)
    assert result.loc[(3, "actual_le_125"), "rows"] == 2
    assert result.loc[(3, "rul_126_plus"), "bias"] == pytest.approx(-75.0)
    assert (3, "rul_025_049") not in result.index
    assert result.loc[(4, "all"), "rmse"] == 0


def test_psi_detects_shift_but_not_resampling():
    rng = np.random.default_rng(0)
    reference = pd.Series(rng.normal(size=5000))
    assert psi(reference, pd.Series(rng.normal(size=5000))) < 0.02
    assert psi(reference, pd.Series(rng.normal(1.0, size=5000))) > 0.2
    constant = pd.Series(np.ones(100))
    assert psi(constant, constant) == pytest.approx(0.0)


def test_drift_reports_each_feature():
    reference = pd.DataFrame({"cycle": np.arange(100), "a": np.arange(100.0), "b": np.zeros(100)})
    result = drift(reference, reference.assign(a=reference.a + 50), ["a", "b"]).set_index("feature")
    assert result.loc["a", "psi"] > 0.2 and result.loc["a", "psi_age_matched"] > 0.2
    assert result.loc["b", "psi"] == pytest.approx(0.0)
    assert result.loc["a", "current_mean"] == pytest.approx(99.5)
    with pytest.raises(ValueError):
        drift(reference.iloc[:0], reference, ["a"])


def test_age_matching_removes_a_pure_lifecycle_shift():
    # Wear rises with age; the fleet is younger, but wear at each age is unchanged.
    rng = np.random.default_rng(1)
    old = pd.DataFrame({"cycle": rng.integers(1, 300, 20000)})
    young = pd.DataFrame({"cycle": rng.integers(1, 150, 5000)})
    old["wear"], young["wear"] = old.cycle + rng.normal(size=len(old)), young.cycle + rng.normal(size=len(young))
    result = drift(old, young, ["wear"]).iloc[0]
    assert result.psi > 0.5 and result.psi_age_matched < 0.02
    with pytest.raises(ValueError, match="outside"):
        drift(young, old, ["wear"])
