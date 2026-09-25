import base64
import hashlib
import hmac
import importlib.util
import json
from pathlib import Path
import re
import urllib.parse

import yaml

from sentinelops import stream

ROOT = Path(__file__).resolve().parents[1]
CONF = {"sentinelops.catalog": "c", "sentinelops.eventhubs_namespace": "evhns-x", "sentinelops.eventhubs_hub": "h",
        "sentinelops.eventhubs_policy": "cmapss-listen", "sentinelops.eventhubs_secret_scope": "scope"}


def trajectory_file(tmp_path, units=3, cycles=4):
    lines = []
    for unit in range(1, units + 1):
        for cycle in range(1, cycles + 1):
            values = [0.0023 * cycle, -0.0003, 100.0, 518.67, 641.71 + unit / 100, 1588.45, 1395.42, 14.62, 21.61,
                      554.85, 2388.01, 9054.42, 1.3, 47.5, 522.16, 2388.06, 8139.62, 8.3803, 0.03, 392, 2388, 100.0,
                      38.86, 23.3735]
            lines.append(" ".join([str(unit), str(cycle), *[str(v) for v in values]]) + "  ")
    path = tmp_path / "test_FD001.txt"
    path.write_text("\n".join(lines) + "\n")
    return path


def test_events_round_trip_the_file_values_exactly(tmp_path):
    path = trajectory_file(tmp_path)
    items = stream.events(path)
    assert len(items) == 12 and items[0]["event_id"] == "CMAPSS-FD001-test-001-001"
    assert items[-1]["event_id"] == "CMAPSS-FD001-test-003-004"
    tokens = path.read_text().split("\n")[5].split()  # unit 2, cycle 2
    decoded = json.loads(json.dumps(items[5]))
    # What Spark casts from the text equals what it parses from the event's JSON number.
    assert [decoded[name] for name in stream.VALUES] == [float(token) for token in tokens[2:]]
    assert (decoded["unit"], decoded["cycle"]) == (2, 2)


def test_each_engine_stays_in_one_partition_in_cycle_order(tmp_path):
    items = stream.events(trajectory_file(tmp_path, units=5))
    assert [stream.partition(u, 2) for u in range(1, 6)] == [0, 1, 0, 1, 0]
    sent = []

    def post(url, data, headers):
        target = int(re.search(r"/partitions/(\d+)/messages", url).group(1))
        sent.extend((target, json.loads(entry["Body"])) for entry in json.loads(data))
        return 201, b""
    log = stream.send(items, "ns", "hub", "cmapss-send", "key", 2, post=post, clock=lambda: 1_000_000)
    assert log["sent"] == len(items) and log["error"] is None and log["per_partition"] == {"0": 12, "1": 8}
    for unit in range(1, 6):
        mine = [(target, event["cycle"]) for target, event in sent if event["unit"] == unit]
        assert {target for target, _ in mine} == {stream.partition(unit, 2)}
        assert [cycle for _, cycle in mine] == [1, 2, 3, 4]


def test_batches_respect_size_and_count_limits_and_keep_order():
    items = [{"event_id": f"e{i}", "value": "x" * 100} for i in range(50)]
    bodies = list(stream.batches(items, max_bytes=1000, max_events=4))
    assert all(len(b) <= 4 and len(stream.body(b)) <= 1000 for b in bodies)
    decoded = [json.loads(json.loads(entry)["Body"]) for b in bodies for entry in b]
    assert decoded == items
    assert json.loads(stream.body(bodies[0]))[0] == {"Body": json.dumps(items[0], separators=(",", ":"))}


def test_sas_token_signs_the_encoded_uri_and_expiry():
    uri = "https://evhns-x.servicebus.windows.net/hub"
    token = stream.sas_token(uri, "cmapss-send", "c2VjcmV0LWtleQ==", 1790300000)
    fields = dict(part.split("=", 1) for part in token.removeprefix("SharedAccessSignature ").split("&"))
    assert fields["sr"] == "https%3A%2F%2Fevhns-x.servicebus.windows.net%2Fhub"
    assert (fields["se"], fields["skn"]) == ("1790300000", "cmapss-send")
    expected = hmac.new(b"c2VjcmV0LWtleQ==", f"{fields['sr']}\n1790300000".encode(), hashlib.sha256).digest()
    assert base64.b64decode(urllib.parse.unquote(fields["sig"])) == expected


def test_send_stops_at_the_first_rejected_batch_without_retrying(tmp_path):
    items = stream.events(trajectory_file(tmp_path, units=4))
    calls = []

    def post(url, data, headers):
        calls.append(url)
        assert headers["Content-Type"] == stream.CONTENT_TYPE and headers["Authorization"].startswith("Shared")
        return (201, b"") if len(calls) == 1 else (401, b"<Error>InvalidSignature</Error>")
    log = stream.send(items, "ns", "hub", "cmapss-send", "key", 2, post=post)
    assert len(calls) == 2 and log["sent"] == 8 and log["error"].startswith("HTTP 401 on partition 1")
    assert "api-version=2014-01" in calls[0] and calls[0].startswith("https://ns.servicebus.windows.net/hub/")


def test_pipeline_rules_match_the_event_shape(load_pipeline, tmp_path):
    pipeline = load_pipeline("pipelines/stream.py", CONF)
    event = stream.events(trajectory_file(tmp_path))[0]
    assert pipeline["EVENT_FIELDS"] == sorted(event)
    assert [f.split()[0] for f in pipeline["EVENT_SCHEMA"].split(", ")] == list(event)
    assert pipeline["VALUES"] == stream.VALUES
    assert set(pipeline["RULES"]) == {"is_json_object", "schema_conforms", "valid_keys", "event_id_matches_keys",
                                      "finite_values"}
    jaas = pipeline["KAFKA"]["kafka.sasl.jaas.config"]
    assert pipeline["KAFKA"]["kafka.bootstrap.servers"] == "evhns-x.servicebus.windows.net:9093"
    assert 'username="$ConnectionString"' in jaas and "SharedAccessKeyName=cmapss-listen;SharedAccessKey=listen-key" in jaas
    assert pipeline["KAFKA"]["kafka.security.protocol"] == "SASL_SSL" and pipeline["KAFKA"]["startingOffsets"] == "earliest"
    for name in ("raw_events", "checked_events", "quarantine", "valid_events", "observations"):
        pipeline[name]()


def test_demo_names_agree_across_bicep_bundle_and_script():
    spec = importlib.util.spec_from_file_location("eventhubs_demo", ROOT / "scripts/eventhubs_demo.py")
    demo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(demo)
    resources = yaml.safe_load((ROOT / "resources/stream.yml").read_text())["resources"]
    conf = resources["pipelines"]["cmapss_stream"]["configuration"]
    assert (conf["sentinelops.eventhubs_namespace"], conf["sentinelops.eventhubs_hub"],
            conf["sentinelops.eventhubs_policy"], conf["sentinelops.eventhubs_secret_scope"]) == \
        (demo.NAMESPACE, demo.HUB, demo.LISTEN, demo.SCOPE)
    bicep = (ROOT / "infra/eventhubs-demo.bicep").read_text()
    assert f"param namespaceName string = '{demo.NAMESPACE}'" in bicep and f"param hubName string = '{demo.HUB}'" in bicep
    assert f"partitionCount: {demo.PARTITIONS}" in bicep
    for policy, right in ((demo.LISTEN, "Listen"), (demo.SEND, "Send")):
        block = bicep[bicep.index(f"name: '{policy}'"):]
        assert re.match(r"name: '[^']+'\s+properties: \{\s+rights: \[\s+'(\w+)'\s+\]", block).group(1) == right
    job = resources["jobs"]["cmapss_stream_ingest"]
    assert [task["task_key"] for task in job["tasks"]] == ["stream", "verify"]
    assert job["tasks"][1]["depends_on"] == [{"task_key": "stream"}]
    assert job["timeout_seconds"] >= sum(task["timeout_seconds"] for task in job["tasks"])
