"""Parity check for the real-time RUL endpoint against the batch inference log.

Sends every fleet row of gold.cmapss_features (the Spark-computed Gold features the model was
trained and batch-scored on) to the serving endpoint, and compares the predictions with
gold.cmapss_predictions for the served version: they should be bit-identical. Also measures
single-row latency on the most at-risk engines' last cycles, and confirms the requests reached
the endpoint's inference table. Manual, for the bounded serving demo; no SQL warehouse.
"""
import argparse
import json
import re
import sys
import time

parser = argparse.ArgumentParser()
parser.add_argument("--catalog", required=True)
parser.add_argument("--schema", required=True)
parser.add_argument("--subset", required=True)
parser.add_argument("--fleet-split", required=True)
parser.add_argument("--endpoint", required=True)
parser.add_argument("--single-row-calls", type=int, required=True)
parser.add_argument("--job-run-id", required=True)
parser.add_argument("--source-root", required=True)
args = parser.parse_args()
# Serverless Python tasks execute via exec(), where __file__ is not defined.
sys.path.insert(0, args.source_root)

import pandas as pd
import requests
from databricks.sdk import WorkspaceClient
from mlflow import MlflowClient
import mlflow
from pyspark.sql import SparkSession, functions as F

from sentinelops import serving
from sentinelops.medallion import KEYS
from sentinelops.model import FEATURES

for identifier in (args.catalog, args.schema):
    if not re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_]*", identifier):
        raise ValueError("Catalog/schema must be simple SQL identifiers")
if not re.fullmatch(r"FD00[1-4]", args.subset) or args.fleet_split == "train":
    raise ValueError("Check one FD001-FD004 subset; training trajectories are not fleet data")
if not re.fullmatch(r"[a-z0-9-]+", args.endpoint):
    raise ValueError("Endpoint must be a serving endpoint name")

started = time.monotonic()
spark = SparkSession.builder.getOrCreate()
w = WorkspaceClient()
endpoint = w.serving_endpoints.get(args.endpoint)
if endpoint.state.ready.value != "READY":
    raise RuntimeError(f"Endpoint {args.endpoint} is {endpoint.state.ready.value}")
(entity,) = endpoint.config.served_entities
model_name, version = entity.entity_name, int(entity.entity_version)
mlflow.set_registry_uri("databricks-uc")
champion = int(MlflowClient().get_model_version_by_alias(model_name, "champion").version)

gold = f"{args.catalog}.gold"
scope = (F.col("subset") == args.subset) & (F.col("split") == args.fleet_split)
# cycle is both a key and the first feature; selecting it twice shifted every request row by one
# column in the first run (959774648252926).
fleet = (spark.table(f"{gold}.cmapss_features").filter(scope)
         .select(*KEYS, *[f for f in FEATURES if f not in KEYS]).orderBy("unit", "cycle").toPandas())
logged = (spark.table(f"{gold}.cmapss_predictions").filter(scope & (F.col("model_version") == version))
          .select(*KEYS, "predicted_rul").toPandas())

sent_after = spark.sql("SELECT current_timestamp() - INTERVAL 1 MINUTE AS t").first().t
url = f"{w.config.host}/serving-endpoints/{args.endpoint}/invocations"
headers = {**w.config.authenticate(), "Content-Type": "application/json"}
session = requests.Session()

# Full fleet in batches: the parity proof.
served, batch_seconds, retried = [], [], []
for start in range(0, len(fleet), serving.BATCH_ROWS):
    rows = fleet.iloc[start:start + serving.BATCH_ROWS]
    text, stats = serving.post(session, url, headers, serving.request_body(rows))
    served.append(rows[KEYS].assign(predicted_rul=serving.predictions(text, len(rows))))
    batch_seconds.append(stats["seconds"])
    retried += stats["retried"]
served = pd.concat(served, ignore_index=True)
result = serving.parity(served, logged, KEYS)

# Single-row latency: the last observed cycle of the engines closest to failure.
last = fleet.loc[fleet.groupby("unit").cycle.idxmax()]
at_risk = last.merge(served, on=KEYS).nsmallest(args.single_row_calls, "predicted_rul")
single_seconds, examples = [], []
for row in at_risk.itertuples(index=False):
    frame = pd.DataFrame([row._asdict()])
    text, stats = serving.post(session, url, headers, serving.request_body(frame))
    single_seconds.append(stats["seconds"])
    examples.append({"unit": int(row.unit), "cycle": int(row.cycle), "predicted_rul": serving.predictions(text, 1)[0]})

# Requests should reach the inference table within seconds (CPU fast path); allow a few minutes.
config = endpoint.ai_gateway.inference_table_config if endpoint.ai_gateway else None
inference = None
if config and config.enabled:
    table = f"{config.catalog_name}.{config.schema_name}.{config.table_name_prefix}_payload"
    for _ in range(12):
        try:
            rows = spark.table(table).filter(F.col("request_time") >= F.lit(sent_after)).count()
        except Exception as error:  # the view may not exist until the first delivery
            rows = f"unavailable: {str(error)[:120]}"
        if isinstance(rows, int) and rows >= len(batch_seconds) + len(single_seconds):
            break
        time.sleep(15)
    inference = {"table": table, "rows_since_check_started": rows,
                 "requests_sent": len(batch_seconds) + len(single_seconds)}

report = {"endpoint": args.endpoint, "model": model_name, "served_version": version, "champion_version": champion,
          "fleet_rows": len(fleet), "logged_rows_for_version": len(logged), "parity": result,
          "batches": {**serving.latency_summary(batch_seconds), "rows_per_batch": serving.BATCH_ROWS,
                      "retried_statuses": retried},
          "single_row": {**serving.latency_summary(single_seconds), "examples": examples},
          "inference_table": inference, "seconds": round(time.monotonic() - started, 1)}
print(json.dumps(report, default=str))
if version != champion or result["rows_unmatched"] or result["bit_identical"] != result["rows_compared"]:
    raise RuntimeError(f"Serving parity failed: {json.dumps(result)}; served v{version}, champion v{champion}")
