"""Summarize one pipeline update's flow metrics and refresh techniques.

Reads the REST events payload on stdin (the CLI list command omits `details`):
  databricks api get "/api/2.0/pipelines/<pipeline>/events?max_results=250" -o json |
    python scripts/pipeline_update_evidence.py <pipeline> <update>
"""
import json
import sys

pipeline_id, update_id = sys.argv[1], sys.argv[2]
payload = json.loads(sys.stdin.buffer.read().decode("utf-8-sig"))
events = payload["events"] if isinstance(payload, dict) else payload
assert "next_page_token" not in payload or any(e.get("origin", {}).get("update_id") != update_id for e in events), "page may truncate update"
events = [e for e in events if e.get("origin", {}).get("update_id") == update_id]
state = next((e["details"]["update_progress"]["state"] for e in events
              if e.get("event_type") == "update_progress"
              and e.get("details", {}).get("update_progress", {}).get("state") in ("COMPLETED", "FAILED", "CANCELED")), None)
flows = {}
# Streaming flows report appended rows on a RUNNING progress event, not on COMPLETED.
appended = {}
for event in events:
    metrics = event.get("details", {}).get("flow_progress", {}).get("metrics") or {}
    if event.get("event_type") == "flow_progress" and event["origin"].get("flow_name"):
        appended.setdefault(event["origin"]["flow_name"], 0)
        appended[event["origin"]["flow_name"]] += metrics.get("num_output_rows", 0)
refresh = {e["origin"].get("flow_name"): e["message"].split("executed as ", 1)[1].split(".")[0]
           for e in events if e.get("event_type") == "planning_information" and "executed as " in e.get("message", "")}
for event in events:
    progress = event.get("details", {}).get("flow_progress")
    if event.get("event_type") != "flow_progress" or not progress or progress.get("status") != "COMPLETED":
        continue
    entry = {"flow": event["origin"].get("flow_name"),
             "refresh": refresh.get(event["origin"].get("flow_name"), "STREAMING_APPEND"),
             "output_rows": progress.get("metrics", {}).get("num_output_rows", appended.get(event["origin"].get("flow_name"))),
             "data_quality": progress.get("data_quality")}
    flows[entry["flow"]] = entry
summary = {"pipeline_id": pipeline_id, "update_id": update_id, "state": state}
if any(f["refresh"] == "NO_OP" for f in flows.values()):
    summary["note"] = ("NO_OP flows rewrote nothing; their reported output_rows is an event metric, "
                       "not the table row count.")
summary["flows"] = sorted(flows.values(), key=lambda f: f["flow"])
print(json.dumps(summary, indent=2))
