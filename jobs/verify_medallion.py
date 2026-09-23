"""On-demand integration assertions; writes a compact evidence file to the volume."""
import argparse
import hashlib
import json
from pathlib import Path
import sys

parser = argparse.ArgumentParser()
parser.add_argument("--catalog", required=True)
parser.add_argument("--schema", required=True)
parser.add_argument("--source-root", required=True)
args = parser.parse_args()
sys.path.insert(0, args.source_root)

import numpy as np
import pandas as pd
from pyspark.sql import SparkSession
from sentinelops.data import read_trajectories
from sentinelops.model import features, labels

spark = SparkSession.builder.getOrCreate()
root = Path(f"/Volumes/{args.catalog}/{args.schema}/landing/cmapss_ingest/v1")
manifest = json.loads((root / "manifest.json").read_text())
for relative, checksum in manifest["files"].items():
    assert hashlib.sha256((root / relative).read_bytes()).hexdigest() == checksum, relative
quality_probe = (root / "trajectories/train_FD001_quality.txt").exists()
counts = {}
for name, expected in {
    "bronze.cmapss_lines": 33727 + (9 if quality_probe else 0),
    "bronze.cmapss_labels": 100,
    "silver.cmapss_observations": 33727,
    "silver.cmapss_quarantine": 5 if quality_probe else 0,
    "silver.cmapss_conflicts": 1 if quality_probe else 0,
    "silver.cmapss_endpoint_labels": 100,
    "gold.cmapss_features": 33727,
    "gold.cmapss_training_labels": 20631,
    "gold.cmapss_test_endpoints": 100,
}.items():
    count = spark.table(f"{args.catalog}.{name}").count()
    assert count == expected, (name, count, expected)
    counts[name] = count

gold = spark.table(f"{args.catalog}.gold.cmapss_features").toPandas()
keys = ["dataset", "subset", "split", "unit", "cycle"]
assert not gold.duplicated(keys).any()
for split in ("train", "test"):
    raw = read_trajectories(root / f"trajectories/{split}_FD001.txt")
    expected = features(raw)
    actual = gold[gold.split == split].sort_values(["unit", "cycle"]).reset_index(drop=True)
    np.testing.assert_allclose(actual[expected.columns], expected, rtol=1e-10, atol=1e-10)
    if split == "train":
        actual_labels = spark.table(f"{args.catalog}.gold.cmapss_training_labels").orderBy("unit", "cycle").toPandas()
        np.testing.assert_array_equal(actual_labels.rul, labels(raw))
expected_labels = pd.read_json(root / "labels/FD001.json", lines=True).sort_values("unit")
actual_labels = spark.table(f"{args.catalog}.gold.cmapss_test_endpoints").orderBy("unit").toPandas()
np.testing.assert_array_equal(actual_labels.rul, expected_labels.rul)
pk = spark.sql(f"SELECT column_name FROM {args.catalog}.information_schema.key_column_usage WHERE table_schema = 'gold' AND table_name = 'cmapss_features' ORDER BY ordinal_position").collect()
assert [r.column_name for r in pk] == keys, pk
evidence_path = root / "verification.json"
previous = json.loads(evidence_path.read_text()) if evidence_path.exists() else None
if previous:
    stable = [name for name in counts if name not in {
        "bronze.cmapss_lines", "silver.cmapss_quarantine", "silver.cmapss_conflicts"}]
    assert all(previous["counts"][name] == counts[name] for name in stable), "Canonical data changed on rerun"
    if previous.get("quality_probe", False) == quality_probe:
        assert previous["counts"] == counts, "Counts changed on no-input rerun"
report = {"counts": counts, "feature_parity": True, "official_endpoint_labels": True,
          "primary_key": keys, "quality_probe": quality_probe,
          "verified_runs": (previous or {}).get("verified_runs", 0) + 1}
if quality_probe:
    conflict = spark.table(f"{args.catalog}.silver.cmapss_conflicts").first()
    assert conflict.unit == 999 and conflict.payload_versions == 2
    invalid = spark.table(f"{args.catalog}.silver.cmapss_quarantine").toPandas()
    assert invalid.source_file.str.endswith("train_FD001_quality.txt").all()
# Reuse this task's compute for a billing snapshot; it may lag the current run.
try:
    report["billing_snapshot"] = [row.asDict() for row in spark.sql("""
        SELECT u.usage_date, u.sku_name, SUM(u.usage_quantity) AS dbus,
               SUM(u.usage_quantity * p.pricing.default) AS list_cost_usd
        FROM system.billing.usage u JOIN system.billing.list_prices p
          ON u.cloud = p.cloud AND u.sku_name = p.sku_name AND u.usage_unit = p.usage_unit
          AND u.usage_start_time >= p.price_start_time
          AND (u.usage_end_time <= p.price_end_time OR p.price_end_time IS NULL)
        WHERE u.workspace_id = '7405619144539463' AND u.usage_date = current_date()
          AND p.currency_code = 'USD'
        GROUP BY u.usage_date, u.sku_name
    """).collect()]
except Exception as error:
    report["billing_snapshot_error"] = str(error)[:1000]
evidence_path.write_text(json.dumps(report, indent=2, default=str))
print(json.dumps(report, default=str))
