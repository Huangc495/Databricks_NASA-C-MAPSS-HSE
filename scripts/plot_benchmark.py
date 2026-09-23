"""Render the verified local FD001 benchmark as a portfolio figure."""
import json
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd

root = Path(__file__).resolve().parents[1]
report = json.loads((root / "artifacts/fd001/metrics.json").read_text())
predictions = pd.read_csv(root / "artifacts/fd001/predictions.csv")
plt.rcParams.update({"font.size": 11, "axes.spines.top": False, "axes.spines.right": False})
fig, axes = plt.subplots(1, 2, figsize=(11, 4.5), layout="constrained")
fig.suptitle("SentinelOps | FD001 remaining useful life", fontsize=17, fontweight="bold")
ax = axes[0]
ax.scatter(predictions.actual_rul, predictions.predicted_rul, s=28, color="#087f8c", alpha=0.75)
maximum = max(predictions.actual_rul.max(), predictions.predicted_rul.max()) + 5
ax.plot([0, maximum], [0, maximum], linestyle="--", color="#64748b", label="Perfect prediction")
ax.set(xlabel="Actual RUL (cycles)", ylabel="Predicted RUL (cycles)", title="100 official test-engine endpoints")
ax.legend(frameon=False)
ax = axes[1]
values = [report["constant_baseline"]["rmse"], report["model"]["rmse"]]
bars = ax.bar(["Constant baseline", "Gradient boosting"], values, color=["#94a3b8", "#087f8c"], width=0.55)
ax.bar_label(bars, fmt="%.2f", padding=5)
ax.set(ylabel="RMSE (cycles; lower is better)", ylim=(0, max(values) * 1.2), title="Same uncapped test labels")
fig.supxlabel("NASA C-MAPSS FD001 • engine-disjoint validation • past-only features • simulated benchmark", fontsize=9, color="#475569")
fig.savefig(root / "docs/fd001-benchmark.png", dpi=160)
plt.close(fig)
