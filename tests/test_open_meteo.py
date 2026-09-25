import datetime as dt
import json
import urllib.error

import pytest

from sentinelops import open_meteo as om

BODY = json.dumps({"latitude": 29.75, "daily": {"time": ["2015-01-01"]}}, separators=(",", ":")).encode()


def test_landing_v1_request_spec_is_frozen():
    # Changing locations, variables, model or units changes every response: that is landing v2
    # (a new prefix and pipeline append flow), never an edit of v1.
    assert om.spec_digest() == "044a9330944ad10c3b2644c6a270936e1a37b96ababae4284b82009d9cad1290"
    assert len(om.LOCATIONS_V1) == 20
    assert len({l.state for l in om.LOCATIONS_V1}) == len({l.location_id for l in om.LOCATIONS_V1}) == 20
    for location in om.LOCATIONS_V1:
        assert location.state == location.state.upper() and location.timezone.startswith("America/")
        assert 24 < location.latitude < 50 and -125 < location.longitude < -66


def test_file_names_are_deterministic_and_parse_back():
    request = om.plan(2016, 2016)[5]
    assert request.file_name == "era5_missouri--kansas-city_2016-01-01_2016-12-31.json"
    assert om.parse_file_name(request.file_name) == {
        "model": "era5", "state": "missouri", "city": "kansas-city", "start": "2016-01-01", "end": "2016-12-31"}
    assert om.plan(2015, 2015)[12].location.location_id == "new-jersey--newark"
    with pytest.raises(ValueError):
        om.parse_file_name("manifest.json")
    assert request.url == (
        "https://archive-api.open-meteo.com/v1/archive?latitude=39.10&longitude=-94.58&start_date=2016-01-01"
        "&end_date=2016-12-31&daily=temperature_2m_max,temperature_2m_mean,apparent_temperature_max"
        "&timezone=America%2FChicago&models=era5&temperature_unit=celsius")


def test_call_weight_follows_open_meteo_examples():
    assert om.call_weight(14) == 1 and om.call_weight(1) == 1
    assert om.call_weight(14, variables=15) == 1.5 and om.call_weight(28, variables=15) == 3.0
    assert round(om.call_weight(365), 2) == 26.07 and round(om.call_weight(366), 2) == 26.14


def test_plan_is_location_major_and_fits_two_runs():
    requests = om.plan(2015, 2025)
    assert len(requests) == 220
    assert [r.location.state for r in requests[:12]] == ["TEXAS"] * 11 + ["FLORIDA"]
    total = sum(r.weight for r in requests)
    assert round(total) == 5740 and om.SAFETY * om.LIMITS["hour"][1] < total < om.SAFETY * om.LIMITS["day"][1]
    with pytest.raises(ValueError):
        om.plan(2016, 2015)


def responses(*items):
    items = list(items)
    calls = []

    def get(url):
        calls.append(url)
        item = items.pop(0)
        if isinstance(item, Exception):
            raise item
        return item
    return get, calls


def test_fetch_returns_the_raw_bytes_and_retries_only_server_errors():
    request = om.plan(2015, 2015)[0]
    sleeps = []
    get, calls = responses((500, b"oops"), urllib.error.URLError("reset"), (200, BODY))
    assert om.fetch(request, get=get, sleep=sleeps.append) == BODY  # Unchanged bytes, not re-serialized.
    assert len(calls) == 3 and sleeps == [10.0, 20.0]
    get, calls = responses((503, b""), (503, b""), (503, b""))
    with pytest.raises(RuntimeError, match="after 3 attempts"):
        om.fetch(request, get=get, sleep=lambda s: None)
    get, calls = responses((429, b'{"error":true,"reason":"Hourly API request limit exceeded"}'))
    with pytest.raises(om.RateLimited, match="Hourly"):
        om.fetch(request, get=get, sleep=lambda s: None)
    assert len(calls) == 1  # Never retried.
    for status, body in ((400, b'{"error":true,"reason":"Invalid date"}'), (200, b'{"error":true,"reason":"x"}'),
                         (200, b"<html>"), (200, b"[1, 2]")):
        get, calls = responses((status, body))
        with pytest.raises(om.Rejected):
            om.fetch(request, get=get, sleep=lambda s: None)
        assert len(calls) == 1


def test_pacer_keeps_any_minute_under_the_limit():
    now = [0.0]
    slept = []

    def sleep(seconds):
        slept.append(round(seconds, 2))
        now[0] += seconds
    pacer = om.Pacer(100, clock=lambda: now[0], sleep=sleep)
    for _ in range(3):
        pacer.wait(40)
        now[0] += 1
    assert slept == [58.01]  # The third 40 waits until the first has left the window.
    assert sum(w for _, w in pacer.window) == 80


def fake_clock():
    state = {"now": dt.datetime(2026, 9, 25, 1, 0, tzinfo=dt.timezone.utc), "mono": 0.0}

    def sleep(seconds):
        state["mono"] += seconds
        state["now"] += dt.timedelta(seconds=seconds)
    return dict(clock=lambda: state["mono"], sleep=sleep, now=lambda: state["now"]), state


def test_run_stops_at_its_budget_and_a_later_run_resumes(tmp_path):
    requests = om.plan(2015, 2019, locations=om.LOCATIONS_V1[:1])  # 5 x ~26 calls
    store = om.LocalStore(tmp_path / "daily")
    get, calls = responses(*[(200, BODY)] * 10)
    timing, state = fake_clock()
    first = om.run(requests, store, max_weighted_calls=60, get=get, **timing)
    assert (first["fetched"], first["remaining"], first["stopped"]) == (2, 3, "budget")
    assert store.existing() == {r.file_name for r in requests[:2]}
    assert first["requests"][0]["sha256"] == __import__("hashlib").sha256(BODY).hexdigest()
    # Ten minutes later the hourly guardrail still counts the first run's calls.
    state["now"] += dt.timedelta(minutes=10)
    second = om.run(requests, store, max_weighted_calls=4000, previous_logs=[first], get=get, **timing)
    assert second["used_before"]["hour"] == pytest.approx(52.21, abs=0.01)
    assert second["already_present"] == 2 and second["fetched"] == 3 and second["stopped"] == "complete"
    third = om.run(requests, store, max_weighted_calls=4000, previous_logs=[first, second], get=get, **timing)
    assert third["fetched"] == 0 and third["weighted_calls"] == 0 and len(calls) == 5


def test_hourly_and_daily_limits_shrink_the_budget(tmp_path):
    now = dt.datetime(2026, 9, 25, 12, tzinfo=dt.timezone.utc)
    log = {"requests": [{"requested_at": (now - dt.timedelta(minutes=30)).isoformat(), "weight": 3990},
                        {"requested_at": (now - dt.timedelta(hours=5)).isoformat(), "weight": 4000},
                        {"requested_at": (now - dt.timedelta(days=2)).isoformat(), "weight": 9999}]}
    assert om.used_calls([log], now) == {"minute": 0, "hour": 3990, "day": 7990}
    result = om.run(om.plan(2015, 2015), om.LocalStore(tmp_path), 4000, previous_logs=[log],
                    get=lambda url: pytest.fail("no budget left for a request"), now=lambda: now)
    assert result["budget"] == pytest.approx(10) and result["stopped"] == "budget" and result["fetched"] == 0


def test_run_records_what_landed_before_a_rate_limit(tmp_path):
    requests = om.plan(2015, 2017, locations=om.LOCATIONS_V1[:1])
    get, _ = responses((200, BODY), (429, b'{"error":true,"reason":"Minutely API request limit exceeded"}'))
    timing, _ = fake_clock()
    log = om.run(requests, om.LocalStore(tmp_path), 4000, get=get, **timing)
    assert log["stopped"] == "error" and log["error"].startswith("RateLimited")
    assert log["fetched"] == 1 and log["remaining"] == 2


def test_local_store_never_overwrites(tmp_path):
    store = om.LocalStore(tmp_path)
    store.write_new("a.json", b"{}")
    with pytest.raises(FileExistsError):
        store.write_new("a.json", b'{"changed": true}')
    assert store.read("a.json") == b"{}" and store.existing() == {"a.json"}
    assert om.refuses_overwrite(store, "a.json") and not list(tmp_path.glob(".*partial"))


def test_volume_store_uploads_without_overwrite():
    from databricks.sdk.errors import AlreadyExists, NotFound

    class Files:
        def __init__(self):
            self.uploads, self.present = [], set()

        def create_directory(self, path):
            pass

        def upload(self, path, contents, overwrite=None):
            assert overwrite is False
            if path in self.present:
                raise AlreadyExists("exists")
            self.present.add(path)
            self.uploads.append((path, contents.read()))

        def list_directory_contents(self, path):
            raise NotFound("no directory yet")

    class Client:
        files = Files()
    store = om.VolumeStore(Client(), "/Volumes/c/s/landing/open_meteo/v1/daily/")
    assert store.existing() == set()
    store.write_new("x.json", b"{}")
    assert Client.files.uploads == [("/Volumes/c/s/landing/open_meteo/v1/daily/x.json", b"{}")]
    with pytest.raises(FileExistsError):
        store.write_new("x.json", b"{}")
