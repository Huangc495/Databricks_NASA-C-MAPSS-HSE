"""On-demand training from the keyed Gold medallion tables; registers a UC challenger.

Run after a completed ingestion update, not concurrently with one: the three Gold
tables are read separately without pinned versions; alignment checks reject most
mixed reads, and logged digests record exactly what was used.
"""
import argparse
import json
from pathlib import Path
import re
import sys
import tempfile

parser = argparse.ArgumentParser()
parser.add_argument("--catalog", required=True)
parser.add_argument("--schema", required=True)
parser.add_argument("--subset", required=True)
parser.add_argument("--source-root", required=True)
args = parser.parse_args()
# Serverless Python tasks execute via exec(), where __file__ is not defined.
sys.path.insert(0, args.source_root)

import mlflow
from mlflow import MlflowClient
from mlflow.models import infer_signature
import numpy as np
import pandas as pd
from pyspark.sql import SparkSession, functions as F

from sentinelops.medallion import digest, endpoint_frame, training_frame
from sentinelops.model import FEATURES, LABEL_CAP, metrics, select
from sentinelops.registry import SKOPS_TRUSTED_TYPES

for identifier in (args.catalog, args.schema):
    if not re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_]*", identifier):
        raise ValueError("Catalog/schema must be simple SQL identifiers")
if not re.fullmatch(r"FD00[1-4]", args.subset):
    raise ValueError("Subset must be one of FD001-FD004")

spark = SparkSession.builder.getOrCreate()
tables = {name: f"{args.catalog}.gold.cmapss_{name}" for name in ("features", "training_labels", "test_endpoints")}
sources = {name: spark.table(table).filter(F.col("subset") == args.subset) for name, table in tables.items()}
frames = {name: source.toPandas() for name, source in sources.items()}
train = training_frame(frames["features"], frames["training_labels"], args.subset)
test = endpoint_frame(frames["test_endpoints"], args.subset)
if not train.rul.between(0, LABEL_CAP).all():
    raise ValueError("Gold training labels violate the label cap contract")
quality = {
    "quarantined_rows": spark.table(f"{args.catalog}.silver.cmapss_quarantine").filter(F.col("subset") == args.subset).count(),
    "conflicting_keys": spark.table(f"{args.catalog}.silver.cmapss_conflicts").filter(F.col("subset") == args.subset).count(),
}

user = spark.sql("SELECT current_user()").first()[0]
mlflow.set_experiment(f"/Users/{user}/sentinelops-fd001")
mlflow.set_registry_uri("databricks-uc")
model_name = f"{args.catalog}.{args.schema}.turbofan_rul"

with mlflow.start_run(run_name=f"{args.subset}-medallion-gold") as run:
    digests = {"features": digest(train[["unit", *FEATURES]]), "training_labels": digest(train[["unit", "cycle", "rul"]]),
               "test_endpoints": digest(test[["unit", *FEATURES, "rul"]])}
    for name, context in (("features", "training"), ("training_labels", "training"), ("test_endpoints", "evaluation")):
        try:
            mlflow.log_input(mlflow.data.from_spark(sources[name], table_name=tables[name], digest=digests[name],
                                                    name=f"{tables[name]}[{args.subset}]"), context=context)
        except Exception as error:  # Lineage metadata must not mask training results.
            mlflow.set_tag(f"lineage_error_{name}", str(error)[:500])

    model, selection = select(train[FEATURES], train.rul, train.unit)
    prediction = model.predict(test[FEATURES])
    baseline = np.repeat(train.rul.mean(), len(test))
    report = {"dataset": "CMAPSS", "subset": args.subset, "source": "gold medallion tables", "tables": tables,
              "digests": digests, "train_rows": len(train), "train_units": int(train.unit.nunique()),
              "test_units": len(test), "training_label_cap": LABEL_CAP,
              "test_labels": "uncapped official endpoint RUL", "quality": quality, "selection": selection,
              "model": metrics(test.rul, prediction), "constant_baseline": metrics(test.rul, baseline)}

    mlflow.log_params({"dataset": "CMAPSS", "subset": args.subset, "feature_source": "gold",
                       "label_cap": LABEL_CAP, "max_leaf_nodes": selection["max_leaf_nodes"],
                       **{f"digest_{name}": value for name, value in digests.items()}})
    mlflow.log_metrics({f"test_{key}": value for key, value in report["model"].items()})
    mlflow.log_metric("validation_rmse", selection["validation_rmse"])
    mlflow.log_metric("baseline_test_rmse", report["constant_baseline"]["rmse"])
    mlflow.log_metrics({f"upstream_{key}": value for key, value in quality.items()})
    mlflow.log_dict(report, "metrics.json")
    with tempfile.TemporaryDirectory() as temporary:
        predictions = Path(temporary) / "predictions.csv"
        pd.DataFrame({"unit": test.unit, "last_cycle": test.cycle, "actual_rul": test.rul,
                      "predicted_rul": prediction}).to_csv(predictions, index=False)
        mlflow.log_artifact(str(predictions))

    example = train[FEATURES].head(5)
    # This model was fitted above from pipeline-validated numeric data. Trust
    # only its known sklearn tree class; never infer trust from an input file.
    info = mlflow.sklearn.log_model(
        model, name="rul_model", input_example=example,
        signature=infer_signature(example, model.predict(example)),
        serialization_format="skops", skops_trusted_types=SKOPS_TRUSTED_TYPES,
    )
    registered = mlflow.register_model(info.model_uri, model_name, tags={
        "training_source": "gold_medallion", "subset": args.subset, "mlflow_run_id": run.info.run_id,
        "feature_table": tables["features"], "feature_digest": digests["features"]})
    MlflowClient().set_registered_model_alias(model_name, "challenger", registered.version)
    report["registered_version"] = registered.version
    print(json.dumps(report, default=str))
