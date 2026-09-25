"""Fetch Open-Meteo daily ERA5 weather into immutable landing (weather_ingest's first task; no Spark).

Plans one request per location and year (sentinelops.open_meteo.LOCATIONS_V1), skips files already
landed, and fetches the rest within this run's weighted-call budget, paced under the per-minute
limit. Earlier runs' fetch logs count against the hourly and daily limits, so a backfill larger than
the budget takes several runs an hour or more apart; each resumes where the last stopped. Raw
responses land unchanged in landing/open_meteo/v1/daily; the full request log (sizes, SHA-256) goes
to landing/open_meteo/_fetch_log/<job run id>.json. Prints one JSON summary line.
"""
import argparse
import json
import re
import sys

parser = argparse.ArgumentParser()
parser.add_argument("--catalog", required=True)
parser.add_argument("--schema", required=True)
parser.add_argument("--first-year", type=int, required=True)
parser.add_argument("--last-year", type=int, required=True)
parser.add_argument("--max-weighted-calls", type=float, required=True)
parser.add_argument("--job-run-id", required=True)
parser.add_argument("--source-root", required=True)
args = parser.parse_args()
# Serverless Python tasks execute via exec(), where __file__ is not defined.
sys.path.insert(0, args.source_root)

from databricks.sdk import WorkspaceClient

from sentinelops import open_meteo as om

for identifier in (args.catalog, args.schema):
    if not re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_]*", identifier):
        raise ValueError("Catalog/schema must be simple SQL identifiers")
if not re.fullmatch(r"[0-9]+", args.job_run_id):
    raise ValueError("Job run ID must be numeric")

client = WorkspaceClient()
root = f"/Volumes/{args.catalog}/{args.schema}/landing/open_meteo"
responses = om.VolumeStore(client, f"{root}/{om.LANDING_VERSION}/daily")
logs = om.VolumeStore(client, f"{root}/_fetch_log")
previous = [json.loads(logs.read(name)) for name in sorted(logs.existing())]

log = om.run(om.plan(args.first_year, args.last_year), responses, args.max_weighted_calls, previous_logs=previous)
log.update(job_run_id=args.job_run_id, first_year=args.first_year, last_year=args.last_year)
logs.write_new(f"{args.job_run_id}.json", (json.dumps(log, indent=2) + "\n").encode())
print(json.dumps({key: log.get(key) for key in (
    "job_run_id", "planned", "already_present", "used_before", "budget", "fetched", "bytes", "weighted_calls",
    "remaining", "stopped", "error", "started_at", "finished_at")}))
if log.get("error"):
    raise RuntimeError(log["error"])
