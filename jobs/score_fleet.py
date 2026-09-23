"""Batch-score fleet observations from Gold with the @champion model into an inference log.

Features come from the Gold table, i.e. the same Spark computation the champion was
trained on. Rows already scored by this model version are skipped, so reruns add
nothing; a new champion version scores the whole fleet once.
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
parser.add_argument("--job-run-id", required=True)
parser.add_argument("--source-root", required=True)
args = parser.parse_args()
# Serverless Python tasks execute via exec(), where __file__ is not defined.
sys.path.insert(0, args.source_root)

import mlflow
from mlflow import MlflowClient
import numpy as np
from pyspark.sql import SparkSession, functions as F

from sentinelops.medallion import KEYS
from sentinelops.model import FEATURES

for identifier in (args.catalog, args.schema):
    if not re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_]*", identifier):
        raise ValueError("Catalog/schema must be simple SQL identifiers")
if not re.fullmatch(r"FD00[1-4]", args.subset) or args.fleet_split == "train":
    raise ValueError("Score one FD001-FD004 subset; training trajectories are not fleet data")

spark = SparkSession.builder.getOrCreate()
mlflow.set_registry_uri("databricks-uc")
name = f"{args.catalog}.{args.schema}.turbofan_rul"
# Resolve the alias once and load that exact version, so a concurrent promotion
# cannot mix model versions within one run.
champion = MlflowClient().get_model_version_by_alias(name, "champion")
version = int(champion.version)
log = f"{args.catalog}.gold.cmapss_predictions"
spark.sql(f"""
    CREATE TABLE IF NOT EXISTS {log} (
      dataset STRING NOT NULL, subset STRING NOT NULL, split STRING NOT NULL,
      unit INT NOT NULL, cycle INT NOT NULL,
      model_name STRING NOT NULL, model_version INT NOT NULL, predicted_rul DOUBLE NOT NULL,
      scored_at TIMESTAMP NOT NULL, scoring_job_run STRING NOT NULL,
      actual_rul INT, label_observed_at TIMESTAMP,
      CONSTRAINT cmapss_predictions_pk PRIMARY KEY (dataset, subset, split, unit, cycle, model_version))
    COMMENT 'Fleet inference log: one row per observation and model version; ground truth arrives later.'
""")

fleet = spark.table(f"{args.catalog}.gold.cmapss_features").filter(
    (F.col("subset") == args.subset) & (F.col("split") == args.fleet_split))
already = spark.table(log).filter(F.col("model_version") == version).select(*KEYS)
pending = fleet.join(already, KEYS, "left_anti").toPandas()
if pending.duplicated(KEYS).any():
    raise ValueError("Duplicate Gold feature keys in the fleet batch")

merged = None
if len(pending):
    model = mlflow.pyfunc.load_model(f"models:/{name}/{version}")
    prediction = np.asarray(model.predict(pending[FEATURES].astype({"cycle": "int64"})), dtype=float)
    if not np.isfinite(prediction).all():
        raise ValueError("Non-finite predictions")
    batch = pending[KEYS].assign(model_name=name, model_version=version, predicted_rul=prediction,
                                 scoring_job_run=args.job_run_id)
    spark.createDataFrame(batch).createOrReplaceTempView("scored_batch")
    merged = spark.sql(f"""
        MERGE INTO {log} t USING scored_batch s
        ON t.dataset = s.dataset AND t.subset = s.subset AND t.split = s.split
           AND t.unit = s.unit AND t.cycle = s.cycle AND t.model_version = s.model_version
        WHEN NOT MATCHED THEN INSERT
          (dataset, subset, split, unit, cycle, model_name, model_version, predicted_rul, scored_at, scoring_job_run)
          VALUES (s.dataset, s.subset, s.split, CAST(s.unit AS INT), CAST(s.cycle AS INT), s.model_name,
                  CAST(s.model_version AS INT), s.predicted_rul, current_timestamp(), s.scoring_job_run)
    """).first().asDict()

report = {"model": name, "champion_version": version, "champion_run": champion.run_id,
          "subset": args.subset, "fleet_split": args.fleet_split, "fleet_rows": fleet.count(),
          "pending_rows": len(pending), "merge": merged,
          "logged_rows_for_version": spark.table(log).filter(F.col("model_version") == version).count()}
print(json.dumps(report, default=str))
