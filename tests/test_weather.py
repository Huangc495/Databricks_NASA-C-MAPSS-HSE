import datetime as dt
import json
from pathlib import Path
import re

import yaml

from sentinelops import open_meteo as om, weather

ROOT = Path(__file__).resolve().parents[1]
NAME = "era5_texas--houston_2015-06-29_2015-07-02.json"


def response(**changes):
    document = {"latitude": 29.75, "longitude": -95.25, "generationtime_ms": 2.8, "utc_offset_seconds": -18000,
                "timezone": "America/Chicago", "timezone_abbreviation": "GMT-5", "elevation": 20.0,
                "daily_units": {"time": "iso8601", **{v: "°C" for v in om.DAILY}},
                "daily": {"time": ["2015-06-29", "2015-06-30", "2015-07-01", "2015-07-02"],
                          # 32.2 C is 89.96 F (not a 90 F day); 35.0 C is exactly 95 F.
                          "temperature_2m_max": [32.2, 32.3, 35.0, 34.9],
                          "temperature_2m_mean": [27.0, 27.5, 28.0, 28.5],
                          # 39.5 C is 103.1 F; 39.4 C is 102.92 F.
                          "apparent_temperature_max": [38.0, 39.0, 39.5, 39.4]}}
    for key, value in changes.items():
        if "." in key:
            outer, inner = key.split(".")
            document[outer] = {**document[outer], inner: value}
        else:
            document[key] = value
    return json.dumps(document).encode()


def failures(rows):
    return [row["failed_rules"] for row in rows]


def test_daily_rows_take_location_from_the_file_name():
    rows = weather.daily_rows(response(), NAME)
    assert failures(rows) == [[]] * 4
    assert {(r["state"], r["city"], r["location_id"]) for r in rows} == {("TEXAS", "Houston", "texas--houston")}
    assert [r["date"] for r in rows] == [dt.date(2015, 6, 29) + dt.timedelta(days=i) for i in range(4)]
    assert rows[2]["temperature_2m_max"] == 35.0 and rows[0]["grid_latitude"] == 29.75
    kansas = weather.daily_rows(response(), NAME.replace("texas--houston", "missouri--kansas-city"))
    assert (kansas[0]["state"], kansas[0]["city"]) == ("MISSOURI", "Kansas City")


def test_rules_quarantine_broken_responses():
    short = weather.daily_rows(response(**{"daily.temperature_2m_mean": [27.0, 27.5, 28.0]}), NAME)
    assert all("complete_series" in f for f in failures(short)) and "has_values" in short[3]["failed_rules"]
    assert all(f == ["celsius_units"] for f in failures(weather.daily_rows(
        response(**{"daily_units.temperature_2m_max": "°F"}), NAME)))
    assert all(f == ["schema_conforms"] for f in failures(weather.daily_rows(response(hourly={}), NAME)))
    assert failures(weather.daily_rows(response(**{"daily.temperature_2m_max": [32.2, 32.3, 99.0, 34.9]}), NAME))[2] \
        == ["plausible_values"]
    assert failures(weather.daily_rows(response(**{"daily.temperature_2m_mean": [27.0, 40.0, 28.0, 28.5]}), NAME))[1] \
        == ["mean_not_above_max"]
    swapped = weather.daily_rows(response(**{"daily.time": ["2015-06-29", "2015-07-01", "2015-06-30",
                                                            "2015-07-02"]}), NAME)
    assert failures(swapped) == [[], ["date_matches_position"], ["date_matches_position"], []]
    assert all("known_file_name" in f for f in failures(weather.daily_rows(response(), "houston.json")))
    empty = weather.daily_rows(response(daily=None), NAME)
    assert len(empty) == 1 and {"complete_series", "has_values"} <= set(empty[0]["failed_rules"])


def test_monthly_counts_hot_days_and_flags_partial_months():
    rows = weather.daily_rows(response(), NAME)
    june_days = [{**rows[0], "date": dt.date(2015, 6, d), "source_file": "full.json"} for d in range(1, 31)]
    gold = weather.monthly(rows + june_days + weather.daily_rows(response(daily=None), NAME))
    assert list(gold.columns) == [name for name, _, _ in weather.MONTHLY_COLUMNS]
    june, july = gold.to_dict("records")
    # full.json sorts after the fixture, so its copies of June 29-30 win (latest landed copy).
    assert (june["month"], june["days"], june["complete"], june["days_max_ge_90f"]) == ("2015-06", 30, True, 0)
    assert (july["month"], july["days"], july["complete"], july["month_start"]) == ("2015-07", 2, False,
                                                                                   dt.date(2015, 7, 1))
    assert (july["days_max_ge_90f"], july["days_max_ge_95f"]) == (2, 1)
    assert (july["days_apparent_ge_90f"], july["days_apparent_ge_103f"]) == (2, 1)
    assert july["mean_max_c"] == (35.0 + 34.9) / 2 and july["highest_apparent_max_c"] == 39.5


def test_pipeline_and_reference_share_one_contract(load_pipeline):
    pipeline = load_pipeline("pipelines/weather.py", {"sentinelops.catalog": "c",
                                                        "sentinelops.weather_landing": "/landing"})
    assert tuple(pipeline["RULES"]) == weather.RULES and pipeline["HOT_DAYS"] == weather.HOT_DAYS
    assert pipeline["DAILY"] == om.DAILY and pipeline["ATTRIBUTION"] == om.ATTRIBUTION
    flat = pipeline["RESPONSE_SCHEMA"]
    while "<" in flat:  # Drop nested types, innermost first.
        flat = re.sub(r"<[^<>]*>", "", flat)
    fields = [re.match(r"\s*(\w+)", part).group(1) for part in flat.split(",")]
    assert tuple(fields) == weather.RESPONSE_FIELDS
    columns = re.findall(r"(\w+) ((?:STRING|DATE|INT|BOOLEAN|DOUBLE)(?: NOT NULL)?) COMMENT '([^']*)'",
                         pipeline["MONTHLY_SCHEMA"])
    assert columns == weather.MONTHLY_COLUMNS
    # The Spark and Python file-name patterns accept and split names identically.
    spark_pattern = re.compile(pipeline["FILE_NAME"])
    for request in om.plan(2015, 2016):
        groups = spark_pattern.fullmatch(request.file_name).groups()
        assert groups == tuple(om.parse_file_name(request.file_name).values())
    assert not spark_pattern.fullmatch("era5_houston_2015-01-01_2015-12-31.json")
    # Every dataset function at least runs (no undefined names) against the stand-ins.
    for name in ("responses", "checked_days", "quarantine", "valid_days", "weather_daily", "state_monthly"):
        pipeline[name]()


def test_weather_job_fetches_ingests_then_verifies():
    resources = yaml.safe_load((ROOT / "resources/weather.yml").read_text())["resources"]
    job = resources["jobs"]["weather_ingest"]
    assert [task["task_key"] for task in job["tasks"]] == ["fetch", "medallion", "verify"]
    for previous, task in zip(job["tasks"], job["tasks"][1:]):
        assert task["depends_on"] == [{"task_key": previous["task_key"]}]
    assert job["timeout_seconds"] >= sum(task["timeout_seconds"] for task in job["tasks"])
    fetch = job["tasks"][0]["spark_python_task"]["parameters"]
    assert float(fetch[fetch.index("--max-weighted-calls") + 1]) <= om.SAFETY * om.LIMITS["hour"][1]
    pipeline = resources["pipelines"]["weather_open_meteo"]
    assert pipeline["configuration"]["sentinelops.weather_landing"].endswith(f"/landing/open_meteo/{om.LANDING_VERSION}")
    assert not pipeline["development"] and not pipeline["continuous"]
