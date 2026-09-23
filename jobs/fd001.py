"""On-demand Databricks bootstrap: Delta tables, tracked model, UC challenger.

Requires an existing UC catalog, serverless jobs, CREATE SCHEMA and model rights.
Retained bootstrap path using batch Delta writes. Training from the Auto Loader/
Lakeflow Gold tables is jobs/train_medallion.py (bundle job cmapss_train).
"""
import argparse
from pathlib import Path
import re
import sys
import tempfile

parser = argparse.ArgumentParser()
parser.add_argument("--catalog", required=True)
parser.add_argument("--schema", required=True)
parser.add_argument("--source-root", required=True)
args = parser.parse_args()
# Serverless Python tasks execute via exec(), where __file__ is not defined.
sys.path.insert(0, args.source_root)

import mlflow
from mlflow import MlflowClient
from mlflow.models import infer_signature
import pandas as pd
from pyspark.sql import SparkSession

from sentinelops.data import read_trajectories
from sentinelops.model import features, labels
from sentinelops.train import run
from sentinelops.registry import SKOPS_TRUSTED_TYPES

for identifier in (args.catalog, args.schema):
    if not re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_]*", identifier):
        raise ValueError("Catalog/schema must be simple SQL identifiers")

spark = SparkSession.builder.getOrCreate()
namespace = f"{args.catalog}.{args.schema}"
spark.sql(f"CREATE SCHEMA IF NOT EXISTS {namespace}")
spark.sql(f"CREATE VOLUME IF NOT EXISTS {namespace}.landing")
data_dir = Path(f"/Volumes/{args.catalog}/{args.schema}/landing/cmapss")
user = spark.sql("SELECT current_user()").first()[0]
mlflow.set_experiment(f"/Users/{user}/sentinelops-fd001")
mlflow.set_registry_uri("databricks-uc")

with tempfile.TemporaryDirectory() as temporary:
    output = Path(temporary)
    with mlflow.start_run(run_name="FD001-engine-disjoint-baseline"):
        model, report = run(data_dir, output)
        train = read_trajectories(data_dir / "train_FD001.txt")
        gold = features(train)
        gold.insert(0, "unit", train.unit)
        gold["rul"] = labels(train)
        for name, frame in (
            ("silver_fd001_train", train),
            ("gold_fd001_features", gold),
            ("gold_fd001_predictions", pd.read_csv(output / "predictions.csv")),
        ):
            spark.createDataFrame(frame).write.format("delta").mode("overwrite").option("overwriteSchema", "true").saveAsTable(f"{namespace}.{name}")
        mlflow.log_params({"dataset": "FD001", "label_cap": 125, "max_leaf_nodes": report["selection"]["max_leaf_nodes"]})
        mlflow.log_metrics({f"test_{key}": value for key, value in report["model"].items()})
        mlflow.log_metric("validation_rmse", report["selection"]["validation_rmse"])
        mlflow.log_metric("baseline_test_rmse", report["constant_baseline"]["rmse"])
        mlflow.log_artifact(str(output / "metrics.json"))
        example = features(train).head(5)
        # This model was fitted above from checksum-verified numeric data. Trust
        # only its known sklearn tree class; never infer trust from an input file.
        info = mlflow.sklearn.log_model(
            model, name="rul_model", input_example=example,
            signature=infer_signature(example, model.predict(example)),
            serialization_format="skops",
            skops_trusted_types=SKOPS_TRUSTED_TYPES,
        )
        registered = mlflow.register_model(info.model_uri, f"{namespace}.turbofan_rul")
        MlflowClient().set_registered_model_alias(f"{namespace}.turbofan_rul", "challenger", registered.version)
