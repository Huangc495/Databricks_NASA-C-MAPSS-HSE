"""Replay C-MAPSS trajectories into Azure Event Hubs as JSON events (bounded streaming demo; no Spark).

The producer runs locally, standing in for an edge gateway: it reads the checksum-verified NASA
file, turns every row into one JSON event and posts them with Event Hubs' REST batch API, signed
with a short-lived SAS token computed in memory. Only the standard library is used. Each engine's
events go to one partition, in cycle order, so a consumer sees every engine's history in order.
The pipeline `cmapss_stream` reads them back through the Kafka endpoint.

Sends are not retried: a timed-out batch may still have been accepted, and a blind retry would
duplicate it. The consumer counts duplicates by event_id, and Silver keeps one copy per key.
"""
import base64
import datetime as dt
import hashlib
import hmac
import json
from pathlib import Path
import time
import urllib.error
import urllib.parse
import urllib.request

from sentinelops.data import COLUMNS, read_trajectories

VALUES = COLUMNS[2:]  # setting_1..3, sensor_1..21
API_VERSION = "2014-01"
CONTENT_TYPE = "application/vnd.microsoft.servicebus.json"
# Standard tier accepts batches up to 1 MB; stay well below it.
MAX_BATCH_BYTES, MAX_BATCH_EVENTS = 256 * 1024, 500


def event_id(dataset: str, subset: str, split: str, unit: int, cycle: int) -> str:
    return f"{dataset}-{subset}-{split}-{unit:03d}-{cycle:03d}"


def events(path: Path, subset: str = "FD001", split: str = "test") -> list[dict]:
    """One event per trajectory row, in (unit, cycle) order. Values stay JSON numbers: Python's
    shortest round-trip repr parses back to the same doubles Spark casts from the original text."""
    frame = read_trajectories(path)
    return [{"event_id": event_id("CMAPSS", subset, split, int(row.unit), int(row.cycle)), "dataset": "CMAPSS",
             "subset": subset, "split": split, "unit": int(row.unit), "cycle": int(row.cycle),
             **{name: float(getattr(row, name)) for name in VALUES}}
            for row in frame.itertuples(index=False)]


def partition(unit: int, partitions: int) -> int:
    """Every event of an engine goes to the same partition, keeping its cycles in order."""
    return (unit - 1) % partitions


def batches(items: list[dict], max_bytes: int = MAX_BATCH_BYTES, max_events: int = MAX_BATCH_EVENTS):
    """REST batch bodies ([{"Body": "<event json>"}, ...]) that keep the input order."""
    batch, size = [], 2
    for item in items:
        entry = json.dumps({"Body": json.dumps(item, separators=(",", ":"))}, separators=(",", ":"))
        if batch and (size + len(entry) + 1 > max_bytes or len(batch) == max_events):
            yield batch
            batch, size = [], 2
        batch.append(entry)
        size += len(entry) + 1
    if batch:
        yield batch


def body(batch: list[str]) -> bytes:
    return ("[" + ",".join(batch) + "]").encode("utf-8")


def sas_token(uri: str, policy: str, key: str, expiry: int) -> str:
    """Event Hubs SAS: HMAC-SHA256 over the URL-encoded resource URI and expiry, keyed with the
    policy key's UTF-8 bytes (Microsoft's REST documentation)."""
    resource = urllib.parse.quote_plus(uri)
    digest = hmac.new(key.encode("utf-8"), f"{resource}\n{expiry}".encode("utf-8"), hashlib.sha256).digest()
    signature = urllib.parse.quote(base64.b64encode(digest), safe="")
    return f"SharedAccessSignature sr={resource}&sig={signature}&se={expiry}&skn={policy}"


def http_post(url: str, data: bytes, headers: dict, timeout: float = 60.0) -> tuple[int, bytes]:
    request = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.read()


def send(items: list[dict], namespace: str, hub: str, policy: str, key: str, partitions: int,
         post=http_post, clock=time.time, token_seconds: int = 3600) -> dict:
    """Post every event to its engine's partition in order. Stops at the first failed batch and
    reports what was accepted; nothing is retried."""
    uri = f"https://{namespace}.servicebus.windows.net/{hub}"
    token = sas_token(uri, policy, key, int(clock()) + token_seconds)
    headers = {"Authorization": token, "Content-Type": CONTENT_TYPE}
    log = {"namespace": namespace, "hub": hub, "partitions": partitions, "events": len(items),
           "started_at": dt.datetime.now(dt.timezone.utc).isoformat(), "batches": [], "sent": 0, "error": None}
    for target in range(partitions):
        mine = [item for item in items if partition(item["unit"], partitions) == target]
        for batch in batches(mine):
            data = body(batch)
            status, reply = post(f"{uri}/partitions/{target}/messages?timeout=60&api-version={API_VERSION}", data,
                                 headers)
            log["batches"].append({"partition": target, "events": len(batch), "bytes": len(data), "status": status})
            if status != 201:
                log["error"] = f"HTTP {status} on partition {target}: {reply[:300].decode('utf-8', 'replace')}"
                log["finished_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
                return log
            log["sent"] += len(batch)
    log["per_partition"] = {str(p): sum(1 for i in items if partition(i["unit"], partitions) == p)
                            for p in range(partitions)}
    log["finished_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
    return log
