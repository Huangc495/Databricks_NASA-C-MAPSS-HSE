"""Independent checks of the Event Hubs demo (cmapss_stream_ingest's last task).

- Bronze holds exactly one event per observation of the replayed file (the batch-ingested
  silver.cmapss_observations rows for the subset and split), and every event_id once.
- Quarantine is empty, and Silver has one row per key with no duplicate deliveries.
- Every streamed observation equals the file-ingested row for the same key, bit for bit, and
  no key is missing on either side.
- Reported, not asserted: whether each partition's offsets are contiguous, and whether each engine's
  events arrived in cycle order (the producer sends every engine to one partition, in order).
Prints one JSON summary line and fails on any count, duplicate or value mismatch.
"""
import argparse
import json
import re
import time

parser = argparse.ArgumentParser()
parser.add_argument("--catalog", required=True)
parser.add_argument("--subset", required=True)
parser.add_argument("--split", required=True)
args = parser.parse_args()

from pyspark.sql import SparkSession

if not re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_]*", args.catalog) or not re.fullmatch(r"FD00[1-4]", args.subset) \
        or args.split not in ("train", "test"):
    raise ValueError("Catalog must be a simple identifier, subset FD001-FD004 and split train or test")

started = time.monotonic()
spark = SparkSession.builder.getOrCreate()
KEYS = ["dataset", "subset", "split", "unit", "cycle"]
VALUES = [f"setting_{i}" for i in range(1, 4)] + [f"sensor_{i}" for i in range(1, 22)]
c = args.catalog
scope = f"subset = '{args.subset}' AND split = '{args.split}'"


def one(sql):
    return spark.sql(sql).first().asDict()


bronze = one(f"""
    SELECT count(*) AS events, count(DISTINCT get_json_object(payload, '$.event_id')) AS event_ids,
           count(DISTINCT partition) AS partitions, min(enqueued_at) AS first_enqueued, max(enqueued_at) AS last_enqueued
    FROM {c}.bronze.cmapss_stream_events""")
partitions = [row.asDict() for row in spark.sql(f"""
    SELECT partition, count(*) AS events, min(offset) AS first_offset, max(offset) AS last_offset,
           max(offset) - min(offset) + 1 = count(*) AS contiguous
    FROM {c}.bronze.cmapss_stream_events GROUP BY partition ORDER BY partition""").collect()]
order = one(f"""
    SELECT count_if(previous_cycle IS NOT NULL AND cycle <= previous_cycle) AS out_of_order,
           count(DISTINCT unit) AS engines, max(engine_partitions) AS max_partitions_per_engine
    FROM (SELECT unit, cycle, lag(cycle) OVER (PARTITION BY unit ORDER BY partition, offset) AS previous_cycle,
                 size(collect_set(partition) OVER (PARTITION BY unit)) AS engine_partitions
          FROM {c}.silver.cmapss_stream_observations WHERE {scope})""")
quarantined = spark.table(f"{c}.silver.cmapss_stream_quarantine").count()
silver = one(f"SELECT count(*) AS rows, max(copies) AS max_copies FROM {c}.silver.cmapss_stream_observations")
same = " AND ".join(f"s.{v} <=> b.{v}" for v in VALUES)
join = " AND ".join(f"s.{k} = b.{k}" for k in KEYS)
parity = one(f"""
    SELECT count_if(s.unit IS NULL) AS missing_from_stream, count_if(b.unit IS NULL) AS not_in_file,
           count_if(s.unit IS NOT NULL AND b.unit IS NOT NULL AND NOT ({same})) AS different_values,
           count_if(s.unit IS NOT NULL AND b.unit IS NOT NULL) AS matched
    FROM (SELECT * FROM {c}.silver.cmapss_stream_observations WHERE {scope}) s
    FULL OUTER JOIN (SELECT * FROM {c}.silver.cmapss_observations WHERE {scope}) b ON {join}""")
expected = parity["matched"] + parity["missing_from_stream"]

assert bronze["events"] == expected == bronze["event_ids"], (bronze, expected)
assert quarantined == 0 and silver["rows"] == expected and silver["max_copies"] == 1, (quarantined, silver)
assert parity["missing_from_stream"] == parity["not_in_file"] == parity["different_values"] == 0, parity

print(json.dumps({"expected_events": expected, **bronze, "quarantined": quarantined, "silver_rows": silver["rows"],
                  **parity, "partitions": partitions, **order, "seconds": round(time.monotonic() - started, 1)},
                 default=str))
