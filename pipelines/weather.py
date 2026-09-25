"""Triggered Lakeflow graph for Open-Meteo daily weather (ERA5) landed by weather_ingest's fetch task.
Plans only; no driver side effects. sentinelops.weather mirrors Silver and Gold in pandas for the
verify task; tests keep the two in step."""
from pyspark import pipelines as dp
from pyspark.sql import functions as F, Window

catalog = spark.conf.get("sentinelops.catalog")
landing = spark.conf.get("sentinelops.weather_landing")
ATTRIBUTION = "Weather data by Open-Meteo.com (CC BY 4.0), ERA5 from the Copernicus Climate Change Service"
DAILY = ("temperature_2m_max", "temperature_2m_mean", "apparent_temperature_max")
# Every field of a response. Anything else (a new field, a changed type) lands in _rescued_data.
RESPONSE_SCHEMA = (
    "latitude DOUBLE, longitude DOUBLE, generationtime_ms DOUBLE, utc_offset_seconds INT, timezone STRING, "
    "timezone_abbreviation STRING, elevation DOUBLE, "
    "daily_units STRUCT<time: STRING, temperature_2m_max: STRING, temperature_2m_mean: STRING, "
    "apparent_temperature_max: STRING>, "
    "daily STRUCT<time: ARRAY<STRING>, temperature_2m_max: ARRAY<DOUBLE>, temperature_2m_mean: ARRAY<DOUBLE>, "
    "apparent_temperature_max: ARRAY<DOUBLE>>")
# <model>_<state>--<city>_<start>_<end>.json, as sentinelops.open_meteo.Request.file_name writes it.
FILE_NAME = r"^([a-z0-9]+)_([a-z-]+?)--([a-z-]+)_([0-9]{4}-[0-9]{2}-[0-9]{2})_([0-9]{4}-[0-9]{2}-[0-9]{2})[.]json$"
# Null-safe, so quarantine (any failure) and the expectation-filtered view partition days exactly.
# utc_offset_seconds is deliberately unused: the API reports the zone's offset at request time.
RULES = {name: f"coalesce(({rule}), false)" for name, rule in {
    "schema_conforms": "_rescued_data IS NULL",
    "known_file_name": "location_id IS NOT NULL",
    "celsius_units": " AND ".join(f"daily_units.{v} = '°C'" for v in DAILY),
    "complete_series": "size(daily.time) = datediff(window_end, window_start) + 1 AND "
                       + " AND ".join(f"size(daily.{v}) = size(daily.time)" for v in DAILY),
    "date_matches_position": "date = date_add(window_start, i)",
    "has_values": " AND ".join(f"{v} IS NOT NULL" for v in DAILY),
    "plausible_values": "temperature_2m_max BETWEEN -60 AND 60 AND temperature_2m_mean BETWEEN -60 AND 60 "
                        "AND apparent_temperature_max BETWEEN -80 AND 75",
    "mean_not_above_max": "temperature_2m_mean <= temperature_2m_max",
}.items()}
HOT_DAYS = (("days_max_ge_90f", "temperature_2m_max", 90), ("days_max_ge_95f", "temperature_2m_max", 95),
            ("days_apparent_ge_90f", "apparent_temperature_max", 90),
            ("days_apparent_ge_103f", "apparent_temperature_max", 103))


def table(layer, name):
    return f"{catalog}.{layer}.{name}"


@dp.table(name=table("bronze", "open_meteo_daily"),
          comment=f"Raw Open-Meteo daily responses, one row per landed file. {ATTRIBUTION}.")
def responses():
    return (spark.readStream.format("cloudFiles").option("cloudFiles.format", "json")
            .option("multiLine", "true")  # One JSON document per file.
            .schema(f"{RESPONSE_SCHEMA}, _rescued_data STRING")
            .option("rescuedDataColumn", "_rescued_data")
            .option("cloudFiles.allowOverwrites", "false")
            .option("pathGlobFilter", "*.json")
            .load(f"{landing}/daily")
            .select("*", F.col("_metadata.file_path").alias("source_file"),
                    F.col("_metadata.file_modification_time").alias("source_modified_at")))


@dp.temporary_view(name="checked_days")
def checked_days():
    part = lambda i: F.expr(f"nullif(regexp_extract(file_name, '{FILE_NAME}', {i}), '')")
    window = lambda i: F.expr(f"to_date(try_to_timestamp(regexp_extract(file_name, '{FILE_NAME}', {i}), "
                              "'yyyy-MM-dd'))")
    days = (spark.read.table(table("bronze", "open_meteo_daily"))
            .withColumn("file_name", F.element_at(F.split("source_file", "/"), -1))
            .select("*", part(1).alias("model"), part(2).alias("state_slug"), part(3).alias("city_slug"),
                    window(4).alias("window_start"), window(5).alias("window_end"))
            .withColumn("location_id", F.concat("state_slug", F.lit("--"), "city_slug"))  # Null unless both.
            # One row per day; a response without a series still yields one (failing) row.
            .selectExpr("*", "posexplode_outer(daily.time) AS (i, day)")
            .selectExpr("*", "to_date(try_to_timestamp(day, 'yyyy-MM-dd')) AS date",
                        *[f"get(daily.{v}, i) AS {v}" for v in DAILY]))
    failed = F.filter(F.array(*[F.when(~F.expr(rule), F.lit(name)) for name, rule in RULES.items()]),
                      lambda name: name.isNotNull())
    return days.withColumn("failed_rules", failed)


@dp.materialized_view(name=table("silver", "weather_quarantine"),
                      comment="Weather days that failed a rule, with the rules they failed.")
def quarantine():
    return spark.read.table("checked_days").filter("size(failed_rules) > 0").drop("daily")


@dp.temporary_view(name="valid_days")
@dp.expect_all_or_drop(RULES)
def valid_days():
    return spark.read.table("checked_days")


silver_schema = ", ".join([
    "location_id STRING NOT NULL", "date DATE NOT NULL", "state STRING", "city STRING", "model STRING",
    "grid_latitude DOUBLE", "grid_longitude DOUBLE", "elevation DOUBLE", "timezone STRING",
    "temperature_2m_max DOUBLE", "temperature_2m_mean DOUBLE", "apparent_temperature_max DOUBLE",
    "source_file STRING", "source_modified_at TIMESTAMP",
    "CONSTRAINT weather_daily_pk PRIMARY KEY (location_id, date)"])


@dp.materialized_view(name=table("silver", "weather_daily"), schema=silver_schema,
                      comment=f"Daily ERA5 weather per location, deg C, one row per location and day. {ATTRIBUTION}.")
def weather_daily():
    # A later landing version may repeat days; the latest landed copy wins.
    latest = Window.partitionBy("location_id", "date").orderBy(F.desc("source_modified_at"), F.desc("source_file"))
    return (spark.read.table("valid_days").withColumn("copy", F.row_number().over(latest)).filter("copy = 1")
            .select("location_id", "date", F.upper(F.regexp_replace("state_slug", "-", " ")).alias("state"),
                    F.initcap(F.regexp_replace("city_slug", "-", " ")).alias("city"), "model",
                    F.col("latitude").alias("grid_latitude"), F.col("longitude").alias("grid_longitude"),
                    "elevation", "timezone", *DAILY, "source_file", "source_modified_at"))


MONTHLY_SCHEMA = ", ".join([
    "state STRING NOT NULL COMMENT 'U.S. state, upper case as in OSHA reports; join on state and month.'",
    "month STRING NOT NULL COMMENT 'Calendar month, YYYY-MM (OSHA event_month format).'",
    "month_start DATE COMMENT 'First day of the month, for time series.'",
    "city STRING COMMENT 'The one city standing in for the whole state (its largest city): a proxy, not a statewide average.'",
    "location_id STRING COMMENT 'Weather location key, <state>--<city>.'",
    "days INT COMMENT 'Days with valid weather in the month.'",
    "complete BOOLEAN COMMENT 'True when every day of the month has valid weather.'",
    "mean_max_c DOUBLE COMMENT 'Average daily maximum 2 m air temperature, deg C (ERA5 grid cell, about 28 km; smoother and usually cooler than a city weather station).'",
    "highest_max_c DOUBLE COMMENT 'Highest daily maximum temperature in the month, deg C.'",
    "mean_c DOUBLE COMMENT 'Average daily mean temperature, deg C.'",
    "mean_apparent_max_c DOUBLE COMMENT 'Average daily maximum apparent (feels-like) temperature, deg C.'",
    "highest_apparent_max_c DOUBLE COMMENT 'Highest daily maximum apparent temperature in the month, deg C.'",
    "days_max_ge_90f INT COMMENT 'Days whose maximum temperature reached 90 deg F (32.2 deg C).'",
    "days_max_ge_95f INT COMMENT 'Days whose maximum temperature reached 95 deg F (35 deg C).'",
    "days_apparent_ge_90f INT COMMENT 'Days whose maximum apparent temperature reached 90 deg F.'",
    "days_apparent_ge_103f INT COMMENT 'Days whose maximum apparent temperature reached 103 deg F (39.4 deg C).'",
    "CONSTRAINT weather_state_monthly_pk PRIMARY KEY (state, month)"])


@dp.materialized_view(name=table("gold", "weather_state_monthly"), schema=MONTHLY_SCHEMA,
                      comment="Monthly weather per state from one representative city, 2015 onwards. "
                              f"{ATTRIBUTION}. Built by the weather_open_meteo pipeline from raw API responses.")
def state_monthly():
    hot = [F.expr(f"CAST(count_if({variable} * 9 / 5 + 32 >= {limit}) AS INT)").alias(name)
           for name, variable, limit in HOT_DAYS]
    return (spark.read.table(table("silver", "weather_daily"))
            .groupBy("state", F.date_format("date", "yyyy-MM").alias("month"), F.trunc("date", "MM").alias("month_start"),
                     "city", "location_id")
            .agg(F.count("*").cast("int").alias("days"), F.avg("temperature_2m_max").alias("mean_max_c"),
                 F.max("temperature_2m_max").alias("highest_max_c"), F.avg("temperature_2m_mean").alias("mean_c"),
                 F.avg("apparent_temperature_max").alias("mean_apparent_max_c"),
                 F.max("apparent_temperature_max").alias("highest_apparent_max_c"), *hot)
            .withColumn("complete", F.col("days") == F.dayofmonth(F.last_day("month_start")))
            .select("state", "month", "month_start", "city", "location_id", "days", "complete", "mean_max_c",
                    "highest_max_c", "mean_c", "mean_apparent_max_c", "highest_apparent_max_c",
                    *[name for name, _, _ in HOT_DAYS]))
