"""Triggered Lakeflow graph for C-MAPSS events from Azure Event Hubs' Kafka endpoint (bounded demo).
Plans only; no driver side effects. Each triggered update reads the events that arrived since the
last one: the checkpoint holds the Kafka offsets, so a rerun appends nothing."""
from pyspark import pipelines as dp
from pyspark.sql import functions as F, Window

catalog = spark.conf.get("sentinelops.catalog")
namespace = spark.conf.get("sentinelops.eventhubs_namespace")
hub = spark.conf.get("sentinelops.eventhubs_hub")
policy = spark.conf.get("sentinelops.eventhubs_policy")  # Listen-only SAS policy; its key is the secret.
key = dbutils.secrets.get(scope=spark.conf.get("sentinelops.eventhubs_secret_scope"), key=policy)
connection = f"Endpoint=sb://{namespace}.servicebus.windows.net/;SharedAccessKeyName={policy};SharedAccessKey={key}"
KAFKA = {
    "kafka.bootstrap.servers": f"{namespace}.servicebus.windows.net:9093",
    "subscribe": hub,
    "kafka.security.protocol": "SASL_SSL",
    "kafka.sasl.mechanism": "PLAIN",
    "kafka.sasl.jaas.config": "kafkashaded.org.apache.kafka.common.security.plain.PlainLoginModule required "
                              f'username="$ConnectionString" password="{connection}";',
    "kafka.request.timeout.ms": "60000",
    "kafka.session.timeout.ms": "30000",
    "startingOffsets": "earliest",
    "failOnDataLoss": "true",  # Events expire after the hub's retention; a gap must fail loudly.
}
KEYS = ["dataset", "subset", "split", "unit", "cycle"]
VALUES = [f"setting_{i}" for i in range(1, 4)] + [f"sensor_{i}" for i in range(1, 22)]
EVENT_SCHEMA = ", ".join(["event_id STRING", "dataset STRING", "subset STRING", "split STRING", "unit INT",
                          "cycle INT", *[f"{v} DOUBLE" for v in VALUES]])
EVENT_FIELDS = sorted(["event_id", *KEYS, *VALUES])
# Null-safe, so quarantine (any failure) and the expectation-filtered view partition events exactly.
RULES = {name: f"coalesce(({rule}), false)" for name, rule in {
    "is_json_object": "event IS NOT NULL",
    "schema_conforms": "array_sort(json_object_keys(payload)) = array(" + ", ".join(f"'{f}'" for f in EVENT_FIELDS) + ")",
    "valid_keys": "event.dataset = 'CMAPSS' AND event.subset RLIKE '^FD00[1-4]$' AND event.split IN ('train', 'test') "
                  "AND event.unit >= 1 AND event.cycle >= 1",
    "event_id_matches_keys": "event.event_id = format_string('%s-%s-%s-%03d-%03d', event.dataset, event.subset, "
                             "event.split, event.unit, event.cycle)",
    "finite_values": " AND ".join(f"event.{v} IS NOT NULL AND NOT isnan(event.{v}) AND abs(event.{v}) < "
                                  "cast('Infinity' AS DOUBLE)" for v in VALUES),
}.items()}


def table(layer, name):
    return f"{catalog}.{layer}.{name}"


@dp.table(name=table("bronze", "cmapss_stream_events"),
          comment="Raw C-MAPSS events from the Event Hubs Kafka endpoint: payload text plus partition, offset and "
                  "enqueued time. Bounded demo; the namespace is deleted afterwards.")
def raw_events():
    return (spark.readStream.format("kafka").options(**KAFKA).load()
            .select(F.col("key").cast("string").alias("key"), F.col("value").cast("string").alias("payload"),
                    "topic", "partition", "offset", F.col("timestamp").alias("enqueued_at")))


@dp.temporary_view(name="checked_events")
def checked_events():
    events = (spark.read.table(table("bronze", "cmapss_stream_events"))
              .withColumn("event", F.from_json("payload", EVENT_SCHEMA)))
    failed = F.filter(F.array(*[F.when(~F.expr(rule), F.lit(name)) for name, rule in RULES.items()]),
                      lambda name: name.isNotNull())
    return events.withColumn("failed_rules", failed)


@dp.materialized_view(name=table("silver", "cmapss_stream_quarantine"),
                      comment="Streamed events that failed a rule, with the rules they failed.")
def quarantine():
    return spark.read.table("checked_events").filter("size(failed_rules) > 0")


@dp.temporary_view(name="valid_events")
@dp.expect_all_or_drop(RULES)
def valid_events():
    return spark.read.table("checked_events")


stream_schema = ", ".join([
    "dataset STRING NOT NULL", "subset STRING NOT NULL", "split STRING NOT NULL", "unit INT NOT NULL",
    "cycle INT NOT NULL", *[f"{v} DOUBLE" for v in VALUES], "event_id STRING", "partition INT", "offset BIGINT",
    "enqueued_at TIMESTAMP", "copies BIGINT",
    "CONSTRAINT cmapss_stream_observations_pk PRIMARY KEY (dataset, subset, split, unit, cycle)"])


@dp.materialized_view(name=table("silver", "cmapss_stream_observations"), schema=stream_schema,
                      comment="C-MAPSS observations received as events, one row per key (the first-enqueued copy); "
                              "copies counts deliveries of the same key.")
def observations():
    first = Window.partitionBy(*KEYS).orderBy("enqueued_at", "partition", "offset")
    return (spark.read.table("valid_events").select("event.*", "partition", "offset", "enqueued_at")
            .withColumn("copies", F.count("*").over(Window.partitionBy(*KEYS)))
            .withColumn("copy", F.row_number().over(first)).filter("copy = 1")
            .select(*KEYS, *VALUES, "event_id", "partition", "offset", "enqueued_at", "copies"))
