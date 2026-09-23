"""Run with python -m sentinelops.train; official test labels are never tuned on."""
import argparse
import json
from pathlib import Path
import pickle

import numpy as np
import pandas as pd

from sentinelops.data import download, read_trajectories
from sentinelops.model import features, fit, labels, metrics


def run(data_dir: Path, output: Path):
    download(data_dir)
    train = read_trajectories(data_dir / "train_FD001.txt")
    test = read_trajectories(data_dir / "test_FD001.txt")
    model, selection = fit(train)
    last = test.groupby("unit").tail(1).sort_values("unit")
    truth = pd.read_csv(data_dir / "RUL_FD001.txt", sep=r"\s+", header=None).iloc[:, 0].to_numpy()
    if len(last) != len(truth) or last.unit.tolist() != list(range(1, len(truth) + 1)):
        raise ValueError("Official test label/unit alignment failed")
    prediction = model.predict(features(test).loc[last.index])
    baseline = np.repeat(labels(train).mean(), len(truth))
    report = {"dataset": "FD001", "train_rows": len(train), "test_rows": len(test), "test_units": len(last), "training_label_cap": 125, "test_labels": "uncapped official endpoint RUL", "selection": selection, "model": metrics(truth, prediction), "constant_baseline": metrics(truth, baseline)}
    output.mkdir(parents=True, exist_ok=True)
    (output / "metrics.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    (output / "model.pkl").write_bytes(pickle.dumps(model))
    pd.DataFrame({"unit": last.unit.to_numpy(), "actual_rul": truth, "predicted_rul": prediction}).to_csv(output / "predictions.csv", index=False)
    print(json.dumps(report, indent=2))
    return model, report


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", type=Path, default=Path("data/cmapss"))
    parser.add_argument("--output", type=Path, default=Path("artifacts/fd001"))
    args = parser.parse_args()
    run(args.data_dir, args.output)
