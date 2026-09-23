import numpy as np
import pytest
from sklearn.ensemble import HistGradientBoostingRegressor
from sentinelops.registry import SKOPS_TRUSTED_TYPES


def test_mlflow_skops_roundtrip_preserves_predictions(tmp_path):
    sklearn_flavor = pytest.importorskip("mlflow.sklearn")
    pytest.importorskip("skops")
    x = np.arange(100, dtype=float).reshape(-1, 1)
    model = HistGradientBoostingRegressor(max_iter=3, random_state=42).fit(x, x[:, 0] ** 0.5)
    path = str(tmp_path / "model")
    sklearn_flavor.save_model(model, path, serialization_format="skops", skops_trusted_types=SKOPS_TRUSTED_TYPES)
    loaded = sklearn_flavor.load_model(path)
    np.testing.assert_allclose(loaded.predict(x), model.predict(x))
