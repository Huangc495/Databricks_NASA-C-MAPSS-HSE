"""Triggered Lakeflow graph. All functions return plans; no driver side effects."""
from pyspark import pipelines as dp
from pyspark.sql import functions as F, Window

catalog = spark.conf.get("sentinelops.catalog")
landing = spark.conf.get("sentinelops.landing")
KEYS = ["dataset", "subset", "split", "unit", "cycle"]
ENGINE = KEYS[:-1]
VALUES = [f"setting_{i}" for i in range(1, 4)] + [f"sensor_{i}" for i in range(1, 22)]
SENSORS = [f"sensor_{i}" for i in (2, 3, 4, 7, 8, 9, 11, 12, 13, 14, 15, 17, 20, 21)]


def table(layer, name):
    return f"{catalog}.{layer}.{name}"


@dp.table(name=table("bronze", "cmapss_lines"))
def raw_lines():
    return (spark.readStream.format("cloudFiles")
            .option("cloudFiles.format", "text")
            .option("pathGlobFilter", "*.txt")
            .option("cloudFiles.allowOverwrites", "false")
            .load(f"{landing}/trajectories")
            .select(F.col("value").alias("raw_line"),
                    F.col("_metadata.file_path").alias("source_file"),
                    F.col("_metadata.file_modification_time").alias("source_modified_at")))


@dp.table(name=table("bronze", "cmapss_labels"))
def raw_labels():
    return (spark.readStream.format("cloudFiles").option("cloudFiles.format", "json")
            .schema("dataset STRING, subset STRING, split STRING, unit INT, rul INT, _rescued_data STRING")
            .option("rescuedDataColumn", "_rescued_data")
            .load(f"{landing}/labels")
            .select("*", F.col("_metadata.file_path").alias("source_file")))


@dp.temporary_view(name="parsed_lines")
def parsed_lines():
    df = (spark.read.table(table("bronze", "cmapss_lines"))
          .withColumn("tokens", F.split(F.trim("raw_line"), r"\s+"))
          .withColumn("dataset", F.lit("CMAPSS"))
          .withColumn("subset", F.regexp_extract("source_file", r"/(?:train|test)_(FD00[1-4])[^/]*\.txt$", 1))
          .withColumn("split", F.regexp_extract("source_file", r"/(train|test)_FD00[1-4][^/]*\.txt$", 1)))
    for index, name in enumerate(["unit", "cycle", *VALUES], 1):
        df = df.withColumn(name, F.expr(f"try_cast(try_element_at(tokens, {index}) AS DOUBLE)"))
    rules = ["size(tokens) = 26", "subset <> ''", "split IN ('train','test')",
             "unit >= 1 AND unit <= 2147483647 AND unit = floor(unit)",
             "cycle >= 1 AND cycle <= 2147483647 AND cycle = floor(cycle)"]
    rules += [f"{c} IS NOT NULL AND NOT isnan({c}) AND abs({c}) < cast('Infinity' AS DOUBLE)" for c in VALUES]
    return (df.withColumn("is_valid", F.coalesce(F.expr(" AND ".join(f"({r})" for r in rules)), F.lit(False)))
            .withColumn("unit", F.expr("try_cast(unit AS INT)"))
            .withColumn("cycle", F.expr("try_cast(cycle AS INT)")))


@dp.materialized_view(name=table("silver", "cmapss_quarantine"))
def quarantine():
    return spark.read.table("parsed_lines").filter("NOT is_valid").drop("tokens")


@dp.temporary_view(name="valid_lines")
@dp.expect_all_or_drop({"valid_trajectory": "is_valid"})
def valid_lines():
    return spark.read.table("parsed_lines")


@dp.materialized_view(name=table("silver", "cmapss_conflicts"))
def conflicts():
    # Equal numeric payloads collapse; conflicting observations never win arbitrarily.
    return (spark.read.table("valid_lines").groupBy(*KEYS)
            .agg(F.countDistinct(F.struct(*VALUES)).alias("payload_versions"))
            .filter("payload_versions > 1"))


@dp.materialized_view(name=table("silver", "cmapss_observations"))
def observations():
    return (spark.read.table("valid_lines")
            .join(spark.read.table(table("silver", "cmapss_conflicts")), KEYS, "left_anti")
            .select(*KEYS, *VALUES).dropDuplicates(KEYS))


@dp.materialized_view(name=table("silver", "cmapss_endpoint_labels"))
@dp.expect_all_or_fail({"official_label": "dataset = 'CMAPSS' AND subset = 'FD001' AND split = 'test' AND unit BETWEEN 1 AND 100 AND rul >= 0 AND _rescued_data IS NULL",
                       "one_label_per_engine": "label_count = 1"})
def endpoint_labels():
    df = spark.read.table(table("bronze", "cmapss_labels"))
    return df.withColumn("label_count", F.count("*").over(Window.partitionBy(*ENGINE)))


feature_columns = ["cycle", *SENSORS] + [c for s in SENSORS for c in (f"{s}_mean_10", f"{s}_delta_5")]
feature_schema = ", ".join([
    "dataset STRING NOT NULL", "subset STRING NOT NULL", "split STRING NOT NULL",
    "unit INT NOT NULL", "cycle INT NOT NULL",
    *[f"{c} DOUBLE" for c in feature_columns if c != "cycle"],
    "CONSTRAINT cmapss_features_pk PRIMARY KEY (dataset, subset, split, unit, cycle)"])


@dp.materialized_view(name=table("gold", "cmapss_features"), schema=feature_schema)
def features():
    df = spark.read.table(table("silver", "cmapss_observations"))
    order = Window.partitionBy(*ENGINE).orderBy("cycle")
    for sensor in SENSORS:
        df = (df.withColumn(f"{sensor}_mean_10", F.avg(sensor).over(order.rowsBetween(-9, 0)))
              .withColumn(f"{sensor}_delta_5", F.coalesce(F.col(sensor) - F.lag(sensor, 5).over(order), F.lit(0.0))))
    return df.select(*KEYS, *[c for c in feature_columns if c != "cycle"])


@dp.materialized_view(name=table("gold", "cmapss_training_labels"))
def training_labels():
    df = spark.read.table(table("silver", "cmapss_observations")).filter("split = 'train'")
    return df.select(*KEYS, F.least(F.lit(125), F.max("cycle").over(Window.partitionBy(*ENGINE)) - F.col("cycle")).alias("rul"))


@dp.materialized_view(name=table("gold", "cmapss_test_endpoints"))
@dp.expect_all_or_fail({"has_official_label": "rul IS NOT NULL"})
def test_endpoints():
    df = spark.read.table(table("gold", "cmapss_features")).filter("split = 'test'")
    df = df.withColumn("last_cycle", F.max("cycle").over(Window.partitionBy(*ENGINE))).filter("cycle = last_cycle").drop("last_cycle")
    return df.join(spark.read.table(table("silver", "cmapss_endpoint_labels")).select(*ENGINE, "rul"), ENGINE, "left")
