"""Threshold alerts on the champion's monitoring snapshots; fails the task when any is breached.

Runs after monitor_fleet.py in the orchestrated cmapss_retrain job. Every check is appended
to gold.cmapss_alerts (breached or not), so the table is an audit trail; a breach then
fails the task, which fails the job run (and triggers the job's failure notifications, if
any are configured). Thresholds are fixed in advance: RMSE relative to the version's first
snapshot, age-matched PSI, and snapshot freshness. No SQL warehouse is needed.
"""
import argparse
import json
import re
import sys

parser = argparse.ArgumentParser()
parser.add_argument("--catalog", required=True)
parser.add_argument("--schema", required=True)
parser.add_argument("--subset", required=True)
parser.add_argument("--fleet-split", required=True)
parser.add_argument("--max-rmse-increase", type=float, required=True)
parser.add_argument("--max-psi", type=float, required=True)
parser.add_argument("--max-snapshot-age-hours", type=float, required=True)
parser.add_argument("--job-run-id", required=True)
parser.add_argument("--source-root", required=True)
args = parser.parse_args()
# Serverless Python tasks execute via exec(), where __file__ is not defined.
sys.path.insert(0, args.source_root)

import mlflow
from mlflow import MlflowClient
from pyspark.sql import SparkSession, functions as F

from sentinelops.monitoring import alert_checks

for identifier in (args.catalog, args.schema):
    if not re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_]*", identifier):
        raise ValueError("Catalog/schema must be simple SQL identifiers")
if not re.fullmatch(r"FD00[1-4]", args.subset) or not re.fullmatch(r"[0-9]+", args.job_run_id):
    raise ValueError("Subset must be FD001-FD004 and the job run ID numeric")

spark = SparkSession.builder.getOrCreate()
gold = f"{args.catalog}.gold"
mlflow.set_registry_uri("databricks-uc")
name = f"{args.catalog}.{args.schema}.turbofan_rul"
version = int(MlflowClient().get_model_version_by_alias(name, "champion").version)
scope = (F.col("subset") == args.subset) & (F.col("fleet_split") == args.fleet_split)
performance = spark.table(f"{gold}.cmapss_model_performance").filter(scope & (F.col("model_version") == version)) \
    .select("segment", "rmse", "computed_at").toPandas()
drift = spark.table(f"{gold}.cmapss_feature_drift").filter(scope) \
    .select("feature", "psi_age_matched", "computed_at").toPandas()
# Snapshot times and "now" both come from Spark, so they share the session time zone.
now = spark.sql("SELECT current_timestamp() AS now").toPandas().now.iloc[0]
checks = alert_checks(performance, drift, now, args.max_rmse_increase, args.max_psi, args.max_snapshot_age_hours)

context = {"model_name": name, "model_version": version, "subset": args.subset, "fleet_split": args.fleet_split,
           "alert_job_run": args.job_run_id}
(spark.createDataFrame(checks.assign(**context))
 .withColumn("checked_at", F.current_timestamp())
 .write.mode("append").saveAsTable(f"{gold}.cmapss_alerts"))

breaches = checks[checks.breached]
report = {"model_version": version, "checks": len(checks), "breaches": breaches.to_dict("records"),
          "thresholds": {"max_rmse_increase": args.max_rmse_increase, "max_psi": args.max_psi,
                         "max_snapshot_age_hours": args.max_snapshot_age_hours},
          "worst": checks.sort_values("value", ascending=False).groupby("check").head(1).to_dict("records")}
print(json.dumps(report, default=str))
if len(breaches):
    raise RuntimeError(f"{len(breaches)} monitoring alert(s): " +
                       "; ".join(f"{r.check}[{r.subject}]={r.value:.4g} > {r.threshold}" for r in breaches.itertuples()))
