"""Validation-gated promotion of the @challenger model version to @champion.

Never reads official test labels. Evidence is each training run's engine-disjoint
validation RMSE, plus a constant baseline recomputed on the same validation engines
from Gold labels whose digest must match what the challenger was trained on.
"""
import argparse
import json
import re
import sys

parser = argparse.ArgumentParser()
parser.add_argument("--catalog", required=True)
parser.add_argument("--schema", required=True)
parser.add_argument("--max-baseline-ratio", type=float, required=True)
parser.add_argument("--tolerance", type=float, required=True)
parser.add_argument("--source-root", required=True)
args = parser.parse_args()
# Serverless Python tasks execute via exec(), where __file__ is not defined.
sys.path.insert(0, args.source_root)

import mlflow
from mlflow import MlflowClient
from mlflow.exceptions import RestException
from pyspark.sql import SparkSession, functions as F

from sentinelops.medallion import digest, label_frame
from sentinelops.promotion import Evidence, constant_baseline_rmse, decide

for identifier in (args.catalog, args.schema):
    if not re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_]*", identifier):
        raise ValueError("Catalog/schema must be simple SQL identifiers")

spark = SparkSession.builder.getOrCreate()
mlflow.set_registry_uri("databricks-uc")
client = MlflowClient()
name = f"{args.catalog}.{args.schema}.turbofan_rul"


def by_alias(alias):
    try:
        return client.get_model_version_by_alias(name, alias)
    except RestException as error:
        if error.error_code == "RESOURCE_DOES_NOT_EXIST":
            return None
        raise


def evidence(version) -> Evidence:
    run = client.get_run(version.run_id)
    selection = mlflow.artifacts.load_dict(f"runs:/{version.run_id}/metrics.json")["selection"]
    signature = mlflow.models.get_model_info(f"models:/{name}/{version.version}").signature
    return Evidence(version=int(version.version), subset=version.tags.get("subset"),
                    training_source=version.tags.get("training_source"),
                    input_columns=tuple(signature.inputs.input_names()) if signature else (),
                    validation_rmse=run.data.metrics["validation_rmse"],
                    fit_units=tuple(selection["fit_units"]), validation_units=tuple(selection["validation_units"]),
                    labels_digest=run.data.params.get("digest_training_labels"))


challenger_version = by_alias("challenger")
if challenger_version is None:
    raise SystemExit("No @challenger version to evaluate")
champion_version = by_alias("champion")
challenger = evidence(challenger_version)
champion = evidence(champion_version) if champion_version else None

baseline, blockers = None, []
if challenger.subset and re.fullmatch(r"FD00[1-4]", challenger.subset) and challenger.labels_digest:
    labels = label_frame(spark.table(f"{args.catalog}.gold.cmapss_training_labels")
                         .filter(F.col("subset") == challenger.subset).toPandas(), challenger.subset)
    if digest(labels[["unit", "cycle", "rul"]]) == challenger.labels_digest:
        baseline = constant_baseline_rmse(labels, challenger.fit_units, challenger.validation_units)
    else:
        blockers.append("Gold training labels changed since the challenger was trained; retrain first")
blockers += decide(challenger, champion, baseline, args.max_baseline_ratio, args.tolerance)

decision = {"model": name, "challenger": challenger.version, "champion_before": champion and champion.version,
            "challenger_validation_rmse": challenger.validation_rmse, "validation_baseline_rmse": baseline,
            "champion_validation_rmse": champion and champion.validation_rmse,
            "validation_units": list(challenger.validation_units), "max_baseline_ratio": args.max_baseline_ratio,
            "tolerance": args.tolerance, "promoted": not blockers, "reasons": blockers}
version = str(challenger.version)
client.set_model_version_tag(name, version, "promotion_decision", "promoted" if not blockers else "rejected")
client.set_model_version_tag(name, version, "promotion_reasons", "; ".join(blockers)[:4000] or "all gates passed")
if not blockers:
    client.set_registered_model_alias(name, "champion", challenger.version)
    client.delete_registered_model_alias(name, "challenger")
decision["champion_after"] = int(client.get_model_version_by_alias(name, "champion").version) if not blockers else decision["champion_before"]
print(json.dumps(decision))
