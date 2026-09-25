"""Silver and Gold semantics of the Open-Meteo weather pipeline, in pandas.

pipelines/weather.py builds the tables in Spark. The weather_ingest job's verify task recomputes
them from the landed raw files with these functions and compares every value, and tests pin the
shared contract (rule names, response fields, hot-day thresholds, Gold columns) against the
pipeline source.
"""
import datetime as dt
import json

from sentinelops.open_meteo import ATTRIBUTION, DAILY, FILE_PATTERN

RESPONSE_FIELDS = ("latitude", "longitude", "generationtime_ms", "utc_offset_seconds", "timezone",
                   "timezone_abbreviation", "elevation", "daily_units", "daily")
# Checked on every day of every landed response; any failure sends the day to quarantine.
RULES = ("schema_conforms", "known_file_name", "celsius_units", "complete_series", "date_matches_position",
         "has_values", "plausible_values", "mean_not_above_max")
PLAUSIBLE = {"temperature_2m_max": (-60, 60), "temperature_2m_mean": (-60, 60),
             "apparent_temperature_max": (-80, 75)}
# Hot days per month: (column, daily variable, threshold in degrees Fahrenheit). Apparent temperature
# is Open-Meteo's (humidity, wind and radiation adjusted); it is not the NWS heat index.
HOT_DAYS = (("days_max_ge_90f", "temperature_2m_max", 90), ("days_max_ge_95f", "temperature_2m_max", 95),
            ("days_apparent_ge_90f", "apparent_temperature_max", 90),
            ("days_apparent_ge_103f", "apparent_temperature_max", 103))
# Identical to the pipeline's MONTHLY_SCHEMA (a test compares them), so no apostrophes.
MONTHLY_COLUMNS = [
    ("state", "STRING NOT NULL", "U.S. state, upper case as in OSHA reports; join on state and month."),
    ("month", "STRING NOT NULL", "Calendar month, YYYY-MM (OSHA event_month format)."),
    ("month_start", "DATE", "First day of the month, for time series."),
    ("city", "STRING", "The one city standing in for the whole state (its largest city): a proxy, not a "
                       "statewide average."),
    ("location_id", "STRING", "Weather location key, <state>--<city>."),
    ("days", "INT", "Days with valid weather in the month."),
    ("complete", "BOOLEAN", "True when every day of the month has valid weather."),
    ("mean_max_c", "DOUBLE", "Average daily maximum 2 m air temperature, deg C (ERA5 grid cell, about 28 km; "
                             "smoother and usually cooler than a city weather station)."),
    ("highest_max_c", "DOUBLE", "Highest daily maximum temperature in the month, deg C."),
    ("mean_c", "DOUBLE", "Average daily mean temperature, deg C."),
    ("mean_apparent_max_c", "DOUBLE", "Average daily maximum apparent (feels-like) temperature, deg C."),
    ("highest_apparent_max_c", "DOUBLE", "Highest daily maximum apparent temperature in the month, deg C."),
    ("days_max_ge_90f", "INT", "Days whose maximum temperature reached 90 deg F (32.2 deg C)."),
    ("days_max_ge_95f", "INT", "Days whose maximum temperature reached 95 deg F (35 deg C)."),
    ("days_apparent_ge_90f", "INT", "Days whose maximum apparent temperature reached 90 deg F."),
    ("days_apparent_ge_103f", "INT", "Days whose maximum apparent temperature reached 103 deg F (39.4 deg C)."),
]
MONTHLY_COMMENT = (f"Monthly weather per state from one representative city, 2015 onwards. {ATTRIBUTION}. "
                   "Built by the weather_open_meteo pipeline from raw API responses.")


def fahrenheit(celsius):
    return celsius * 9 / 5 + 32  # The pipeline's exact expression order, so thresholds agree bit-for-bit.


def _date(text):
    try:
        return dt.date.fromisoformat(text)
    except (TypeError, ValueError):
        return None


def daily_rows(body: bytes, file_name: str) -> list[dict]:
    """One row per day of one landed response, with the rules the row fails ("failed_rules")."""
    document = json.loads(body)
    match = FILE_PATTERN.fullmatch(file_name)
    names = match.groupdict() if match else {}
    start, end = _date(names.get("start")), _date(names.get("end"))
    daily, units = document.get("daily") or {}, document.get("daily_units") or {}
    series = [daily.get(name) for name in ("time",) + DAILY]
    response_rules = {
        "schema_conforms": set(document) <= set(RESPONSE_FIELDS) and set(daily) <= {"time", *DAILY}
                           and set(units) <= {"time", *DAILY},
        "known_file_name": bool(match),
        "celsius_units": all(units.get(name) == "°C" for name in DAILY),
        "complete_series": bool(start and end) and all(isinstance(s, list) for s in series)
                           and len({len(s) for s in series}) == 1 and len(series[0]) == (end - start).days + 1,
    }
    times = series[0] if isinstance(series[0], list) else [None]
    rows = []
    for i, text in enumerate(times):
        values = {name: (s[i] if isinstance(s, list) and i < len(s) else None) for name, s in zip(DAILY, series[1:])}
        day = _date(text)
        rules = dict(response_rules)
        rules["date_matches_position"] = bool(day and start) and day == start + dt.timedelta(days=i)
        rules["has_values"] = all(isinstance(v, (int, float)) for v in values.values())
        rules["plausible_values"] = rules["has_values"] and all(
            PLAUSIBLE[name][0] <= value <= PLAUSIBLE[name][1] for name, value in values.items())
        rules["mean_not_above_max"] = rules["has_values"] and \
            values["temperature_2m_mean"] <= values["temperature_2m_max"]
        rows.append({
            "location_id": f"{names['state']}--{names['city']}" if match else None,
            "date": day, "state": names["state"].replace("-", " ").upper() if match else None,
            "city": names["city"].replace("-", " ").title() if match else None, "model": names.get("model"),
            "grid_latitude": document.get("latitude"), "grid_longitude": document.get("longitude"),
            "elevation": document.get("elevation"), "timezone": document.get("timezone"), **values,
            "source_file": file_name, "failed_rules": [name for name in RULES if not rules[name]]})
    return rows


def monthly(rows: list[dict]):
    """Gold weather_state_monthly from valid daily rows (the latest landed copy of each day wins)."""
    import pandas as pd

    frame = pd.DataFrame([r for r in rows if not r["failed_rules"]])
    frame = frame.sort_values("source_file").drop_duplicates(["location_id", "date"], keep="last")
    frame["month"] = frame["date"].map(lambda d: d.strftime("%Y-%m"))
    for column, variable, limit in HOT_DAYS:
        frame[column] = fahrenheit(frame[variable]) >= limit
    grouped = frame.groupby(["state", "month", "city", "location_id"], as_index=False).agg(
        days=("date", "size"), mean_max_c=("temperature_2m_max", "mean"),
        highest_max_c=("temperature_2m_max", "max"), mean_c=("temperature_2m_mean", "mean"),
        mean_apparent_max_c=("apparent_temperature_max", "mean"),
        highest_apparent_max_c=("apparent_temperature_max", "max"),
        **{column: (column, "sum") for column, _, _ in HOT_DAYS})
    grouped["month_start"] = grouped["month"].map(lambda m: dt.date.fromisoformat(f"{m}-01"))
    grouped["complete"] = grouped["days"] == grouped["month_start"].map(
        lambda d: ((d.replace(day=28) + dt.timedelta(days=4)).replace(day=1) - d).days)
    names = [name for name, _, _ in MONTHLY_COLUMNS]
    return grouped[names].sort_values(["state", "month"]).reset_index(drop=True)
