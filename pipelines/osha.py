"""Triggered Lakeflow graph for minimized OSHA Severe Injury Reports. Plans only; no driver side effects."""
from pyspark import pipelines as dp
from pyspark.sql import functions as F, Window

catalog = spark.conf.get("sentinelops.catalog")
landing = spark.conf.get("sentinelops.osha_landing")
# Later landing versions (comma-separated prefixes), e.g. osha_sir/v2 with stronger masking. Each is
# its own append flow into Bronze, so the v1 flow and its checkpoint stay untouched; Silver keeps
# the latest landed copy of each report.
later_landings = [p.strip() for p in spark.conf.get("sentinelops.osha_landing_later", "").split(",") if p.strip()]
FIELDS = ("report_id BIGINT, osha_id STRING, event_month STRING, state STRING, naics STRING, federal_state INT, "
          "hospitalized INT, amputation INT, loss_of_eye INT, inspected BOOLEAN, narrative STRING, "
          "nature_code STRING, nature_title STRING, body_part_code STRING, body_part_title STRING, "
          "event_code STRING, event_title STRING, source_code STRING, source_title STRING, "
          "secondary_source_code STRING, secondary_source_title STRING")
# Null-safe, so quarantine (NOT valid) and the expectation-filtered view partition rows exactly.
RULES = {name: f"coalesce(({rule}), false)" for name, rule in {
    "has_report_id": "report_id IS NOT NULL AND report_id > 0",
    "schema_conforms": "_rescued_data IS NULL",
    "valid_month": "event_month RLIKE '^20[0-9]{2}-(0[1-9]|1[0-2])$'",
    "has_state": "state IS NOT NULL AND length(state) > 1",
    # Industry is optional metadata: blank (unknown) and sector ranges such as 48-49 are valid.
    "valid_naics": "naics RLIKE '^([0-9]{2,6}|[0-9]{2}-[0-9]{2})?$'",
    "has_narrative": "length(trim(narrative)) >= 20",
    "has_event_code": "event_code IS NOT NULL",
}.items()}


def table(layer, name):
    return f"{catalog}.{layer}.{name}"


def landed_reports(prefix):
    return (spark.readStream.format("cloudFiles").option("cloudFiles.format", "json")
            .schema(f"{FIELDS}, _rescued_data STRING")
            .option("rescuedDataColumn", "_rescued_data")
            .option("cloudFiles.allowOverwrites", "false")
            .option("pathGlobFilter", "*.jsonl")
            .load(f"{prefix}/reports")
            .select("*", F.col("_metadata.file_path").alias("source_file"),
                    F.col("_metadata.file_modification_time").alias("source_modified_at")))


@dp.table(name=table("bronze", "osha_sir_reports"))
def raw_reports():
    return landed_reports(landing)


for later in later_landings:
    # Flow names identify checkpoints: never rename one, and never reuse a name for another prefix.
    @dp.append_flow(target=table("bronze", "osha_sir_reports"), name=f"osha_sir_{later.rstrip('/').rsplit('/', 1)[-1]}")
    def later_reports(prefix=later):
        return landed_reports(prefix)


@dp.temporary_view(name="checked_reports")
def checked_reports():
    valid = F.expr(" AND ".join(RULES.values()))
    return spark.read.table(table("bronze", "osha_sir_reports")).withColumn("is_valid", valid)


@dp.materialized_view(name=table("silver", "osha_quarantine"))
def quarantine():
    return spark.read.table("checked_reports").filter("NOT is_valid")


@dp.temporary_view(name="valid_reports")
@dp.expect_all_or_drop(RULES)
def valid_reports():
    return spark.read.table("checked_reports").drop("is_valid")


@dp.materialized_view(name=table("silver", "osha_incidents"))
def incidents():
    # A republished OSHA snapshot repeats earlier reports; the latest landed copy wins.
    latest = Window.partitionBy("report_id").orderBy(F.desc("source_modified_at"), F.desc("source_file"))
    return (spark.read.table("valid_reports").withColumn("copy", F.row_number().over(latest))
            .filter("copy = 1").drop("copy", "_rescued_data"))


documents_schema = ", ".join([
    "report_id BIGINT NOT NULL", "osha_id STRING", "event_month STRING", "event_year INT", "state STRING",
    "naics STRING", "naics_sector STRING", "hospitalized INT", "amputation INT", "loss_of_eye INT",
    "inspected BOOLEAN", "nature_title STRING", "body_part_title STRING", "event_title STRING",
    "source_title STRING", "secondary_source_title STRING", "nature_code STRING", "body_part_code STRING",
    "event_code STRING", "source_code STRING", "narrative STRING", "document STRING", "document_sha256 STRING",
    "CONSTRAINT osha_documents_pk PRIMARY KEY (report_id)"])


@dp.materialized_view(name=table("gold", "osha_documents"), schema=documents_schema)
def documents():
    # One retrievable document per incident (narratives are at most a few hundred
    # words). The short coded header helps category questions; report_id is the citation.
    df = spark.read.table(table("silver", "osha_incidents")).withColumn("naics", F.expr("nullif(naics, '')"))
    header = F.concat_ws(" | ", F.concat(F.lit("Event: "), F.col("event_title")),
                         F.concat(F.lit("Injury: "), F.col("nature_title"), F.lit(", "), F.col("body_part_title")),
                         F.concat(F.lit("Source: "), F.col("source_title")),
                         F.concat_ws(", ", F.concat(F.lit("NAICS "), F.col("naics")), F.col("state"), F.col("event_month")))
    document = F.concat(header, F.lit("\n"), F.col("narrative"))
    return df.select(
        "report_id", "osha_id", "event_month", F.substring("event_month", 1, 4).cast("int").alias("event_year"),
        "state", "naics", F.substring("naics", 1, 2).alias("naics_sector"), "hospitalized", "amputation",
        "loss_of_eye", "inspected", "nature_title", "body_part_title", "event_title", "source_title",
        "secondary_source_title", "nature_code", "body_part_code", "event_code", "source_code", "narrative",
        document.alias("document"), F.sha2(document, 256).alias("document_sha256"))
