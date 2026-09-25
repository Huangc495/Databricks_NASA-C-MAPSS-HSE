"""Local steps of the bounded Event Hubs demo. Keys are read with the signed-in Azure CLI inside this
process and are never printed, written to disk or passed on a command line.

  put-secret  store the listen policy's key in a Databricks-backed secret scope (the pipeline reads it)
  produce     replay FD001 test trajectories into the hub (sentinelops.stream); refuses to send twice

  . ./scripts/Use-SentinelOps.ps1
  .venv/Scripts/python.exe scripts/eventhubs_demo.py put-secret
  .venv/Scripts/python.exe scripts/eventhubs_demo.py produce
"""
import argparse
import json
from pathlib import Path
import shutil
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sentinelops import data, stream

RESOURCE_GROUP, NAMESPACE, HUB = "rg-sentinelops-dev", "evhns-sentinelops-7s5fwy", "cmapss-telemetry"
LISTEN, SEND, SCOPE, PARTITIONS = "cmapss-listen", "cmapss-send", "sentinelops-eventhubs", 2


def policy_key(policy: str) -> str:
    az = shutil.which("az") or shutil.which("az.cmd")
    result = subprocess.run([az, "eventhubs", "namespace", "authorization-rule", "keys", "list",
                             "--resource-group", RESOURCE_GROUP, "--namespace-name", NAMESPACE, "--name", policy,
                             "--query", "primaryKey", "-o", "tsv"], capture_output=True, text=True, timeout=120)
    key = result.stdout.strip()
    if result.returncode or not key:
        raise RuntimeError(f"Could not read the {policy} key: {result.stderr.strip()[-300:]}")
    return key


def put_secret() -> None:
    from databricks.sdk import WorkspaceClient

    client = WorkspaceClient()
    if SCOPE not in {scope.name for scope in client.secrets.list_scopes()}:
        client.secrets.create_scope(SCOPE)
    client.secrets.put_secret(SCOPE, LISTEN, string_value=policy_key(LISTEN))
    print(json.dumps({"scope": SCOPE, "keys": [s.key for s in client.secrets.list_secrets(SCOPE)]}))


def produce(source: Path, log_path: Path) -> None:
    if log_path.exists():
        raise SystemExit(f"{log_path} exists: these events were already sent. Delete it only if the hub is new.")
    data.download(source.parent)  # Rechecks the NASA archive's MD5 and re-extracts the file.
    items = stream.events(source)
    log = stream.send(items, NAMESPACE, HUB, SEND, policy_key(SEND), PARTITIONS)
    log["source"] = f"{source.as_posix()} (NASA C-MAPSS, archive MD5 {data.MD5})"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_bytes((json.dumps(log, indent=2) + "\n").encode("utf-8"))
    print(json.dumps({key: log[key] for key in ("events", "sent", "error", "per_partition") if key in log}
                     | {"batches": len(log["batches"]), "log": log_path.as_posix()}))
    if log["error"]:
        raise SystemExit(1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["put-secret", "produce"])
    parser.add_argument("--source", type=Path, default=Path("data/cmapss/test_FD001.txt"))
    parser.add_argument("--log", type=Path, default=Path(f"artifacts/eventhubs/{NAMESPACE}-{HUB}.json"))
    args = parser.parse_args()
    put_secret() if args.command == "put-secret" else produce(args.source, args.log)
