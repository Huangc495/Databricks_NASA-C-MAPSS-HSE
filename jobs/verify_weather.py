"""Independent checks of the weather pipeline (weather_ingest's last task).

- Every landed response still has the SHA-256 its fetch log recorded: bytes unchanged since landing.
- The Files API refuses to overwrite a landed file (a probe re-sends one file's own bytes with
  overwrite=False).
- Bronze holds exactly one row per landed file: no file was ingested twice.
- Quarantine, Silver and Gold equal a pandas recomputation from the raw files (sentinelops.weather):
  every key, every daily value bit-for-bit, and every monthly value (averages to 1e-9, since Spark
  and pandas sum in different orders).
Prints one JSON summary line and fails on any mismatch.
"""
import argparse
import hashlib
import json
from pathlib import Path
import re
import sys
import time

parser = argparse.ArgumentParser()
parser.add_argument("--catalog", required=True)
parser.add_argument("--schema", required=True)
parser.add_argument("--source-root", required=True)
args = parser.parse_args()
# Serverless Python tasks execute via exec(), where __file__ is not defined.
sys.path.insert(0, args.source_root)

import numpy as np
import pandas as pd
from databricks.sdk import WorkspaceClient
from pyspark.sql import SparkSession

from sentinelops import open_meteo as om, weather

for identifier in (args.catalog, args.schema):
    if not re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_]*", identifier):
        raise ValueError("Catalog/schema must be simple SQL identifiers")

started = time.monotonic()
spark = SparkSession.builder.getOrCreate()
root = Path(f"/Volumes/{args.catalog}/{args.schema}/landing/open_meteo")
files = sorted((root / om.LANDING_VERSION / "daily").glob("*.json"))
if not files:
    raise RuntimeError("Nothing landed yet")
recorded = {}
for log_path in sorted((root / "_fetch_log").glob("*.json")):
    for item in json.loads(log_path.read_text())["requests"]:
        if "skipped" not in item:
            recorded.setdefault(item["file"], item["sha256"])
raw = {path.name: path.read_bytes() for path in files}
changed = sorted(name for name, body in raw.items() if hashlib.sha256(body).hexdigest() != recorded.get(name))
assert not changed, f"Landed files differ from their fetch log (or have none): {changed[:5]}"
assert om.refuses_overwrite(om.VolumeStore(WorkspaceClient(), str(root / om.LANDING_VERSION / "daily")),
                            files[0].name), "The Files API accepted an overwrite of a landed file"

rows = [row for name, body in raw.items() for row in weather.daily_rows(body, name)]
failed = [row for row in rows if row["failed_rules"]]
expected_daily = (pd.DataFrame([row for row in rows if not row["failed_rules"]])
                  .sort_values("source_file").drop_duplicates(["location_id", "date"], keep="last"))
expected_monthly = weather.monthly(rows)


def table(layer, name):
    return spark.table(f"{args.catalog}.{layer}.{name}")


bronze = table("bronze", "open_meteo_daily")
bronze_rows, bronze_files = bronze.count(), bronze.select("source_file").distinct().count()
assert bronze_rows == bronze_files == len(files), (bronze_rows, bronze_files, len(files))
quarantined = table("silver", "weather_quarantine").count()
assert quarantined == len(failed), (quarantined, len(failed))

keys = ["location_id", "date"]
columns = keys + ["state", "city", *om.DAILY]
silver = table("silver", "weather_daily").select(*columns).toPandas()
silver["date"] = pd.to_datetime(silver["date"]).dt.date
silver = silver.sort_values(keys).reset_index(drop=True)
expected = expected_daily[columns].sort_values(keys).reset_index(drop=True)
assert len(silver) == len(expected) and not silver.duplicated(keys).any(), (len(silver), len(expected))
assert silver.equals(expected), "Silver weather_daily differs from the raw responses"

names = [name for name, _, _ in weather.MONTHLY_COLUMNS]
gold = table("gold", "weather_state_monthly").select(*names).toPandas()
gold["month_start"] = pd.to_datetime(gold["month_start"]).dt.date
gold = gold.sort_values(["state", "month"]).reset_index(drop=True)
assert len(gold) == len(expected_monthly) and not gold.duplicated(["state", "month"]).any()
floats = [name for name, kind, _ in weather.MONTHLY_COLUMNS if kind == "DOUBLE"]
exact = [name for name in names if name not in floats]
assert gold[exact].astype(str).equals(expected_monthly[exact].astype(str)), "Gold keys or counts differ"
difference = float(np.max(np.abs(gold[floats].to_numpy(float) - expected_monthly[floats].to_numpy(float))))
assert np.allclose(gold[floats].to_numpy(float), expected_monthly[floats].to_numpy(float), rtol=1e-9, atol=1e-9)

print(json.dumps({
    "landed_files": len(files), "landed_bytes": sum(len(body) for body in raw.values()),
    "sha256_verified": len(files), "overwrite_refused": True, "bronze_rows": bronze_rows,
    "silver_days": len(silver), "quarantined": quarantined, "gold_months": len(gold),
    "complete_months": int(gold["complete"].sum()), "states": int(gold["state"].nunique()),
    "first_month": gold["month"].min(), "last_month": gold["month"].max(),
    "max_float_difference": difference, "seconds": round(time.monotonic() - started, 1)}))
