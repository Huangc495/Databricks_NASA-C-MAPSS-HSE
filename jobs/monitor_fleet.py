"""Merge delayed ground truth into the fleet inference log, then snapshot performance and drift.

Simulation note: the NASA test engines play the in-service fleet. An engine's ground
truth arrives with its official endpoint label; RUL at an earlier cycle c is then
endpoint RUL + (last observed cycle - c), uncapped. These metrics describe operation
and must not be used to choose or promote models (see jobs/promote.py).
"""
import argparse
import json
import re
import sys

parser = argparse.ArgumentParser()
parser.add_argument("--catalog", required=True)
parser.add_argument("--subset", required=True)
parser.add_argument("--fleet-split", required=True)
parser.add_argument("--job-run-id", required=True)
parser.add_argument("--source-root", required=True)
args = parser.parse_args()
# Serverless Python tasks execute via exec(), where __file__ is not defined.
sys.path.insert(0, args.source_root)

from pyspark.sql import SparkSession, functions as F

from sentinelops.medallion import ENGINE
from sentinelops.model import FEATURES
from sentinelops.monitoring import drift, performance

if not re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_]*", args.catalog):
    raise ValueError("Catalog must be a simple SQL identifier")
if not re.fullmatch(r"FD00[1-4]", args.subset) or args.fleet_split == "train":
    raise ValueError("Monitor one FD001-FD004 subset; training trajectories are not fleet data")

spark = SparkSession.builder.getOrCreate()
gold = f"{args.catalog}.gold"
log, endpoints = f"{gold}.cmapss_predictions", f"{gold}.cmapss_test_endpoints"

labels = spark.sql(f"""
    MERGE INTO {log} t
    USING (
      SELECT p.dataset, p.subset, p.split, p.unit, p.cycle, e.rul + e.cycle - p.cycle AS actual_rul
      FROM (SELECT DISTINCT dataset, subset, split, unit, cycle FROM {log} WHERE actual_rul IS NULL) p
      JOIN {endpoints} e ON p.dataset = e.dataset AND p.subset = e.subset AND p.split = e.split AND p.unit = e.unit
      WHERE e.rul IS NOT NULL AND p.cycle <= e.cycle
    ) s
    ON t.dataset = s.dataset AND t.subset = s.subset AND t.split = s.split AND t.unit = s.unit AND t.cycle = s.cycle
    WHEN MATCHED AND t.actual_rul IS NULL THEN
      UPDATE SET actual_rul = s.actual_rul, label_observed_at = current_timestamp()
""").first().asDict()

last_cycle = spark.table(endpoints).select(*ENGINE, F.col("cycle").alias("last_cycle"))
labelled = (spark.table(log)
            .filter((F.col("subset") == args.subset) & (F.col("split") == args.fleet_split) & F.col("actual_rul").isNotNull())
            .join(last_cycle, ENGINE)
            .select("model_name", "model_version", "predicted_rul", "actual_rul", (F.col("cycle") == F.col("last_cycle")).alias("is_endpoint"))
            .toPandas())
if labelled.empty:
    raise SystemExit("No labelled predictions to monitor yet")
names = labelled.groupby("model_version").model_name.first()
metrics = performance(labelled).assign(model_name=lambda f: f.model_version.map(names))

features = (spark.table(f"{gold}.cmapss_features").filter(F.col("subset") == args.subset)
            .select("split", *FEATURES).toPandas())
shift = drift(features[features.split == "train"], features[features.split == args.fleet_split], FEATURES)

context = {"subset": args.subset, "fleet_split": args.fleet_split, "monitoring_job_run": args.job_run_id}
for frame, table in ((metrics, f"{gold}.cmapss_model_performance"), (shift, f"{gold}.cmapss_feature_drift")):
    (spark.createDataFrame(frame.assign(**context))
     .withColumn("computed_at", F.current_timestamp())
     .write.mode("append").saveAsTable(table))

report = {"labels_merge": labels, "labelled_rows": len(labelled),
          "performance": metrics[metrics.segment.isin(["all", "endpoint", "actual_le_125"])].to_dict("records"),
          "drift_features_over_0_2": {"raw": int((shift.psi > 0.2).sum()),
                                      "age_matched": int((shift.psi_age_matched > 0.2).sum()), "of": len(shift)},
          "top_drift": shift.nlargest(5, "psi_age_matched")[["feature", "psi", "psi_age_matched"]].to_dict("records")}
print(json.dumps(report, default=str))
