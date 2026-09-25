"""Open-Meteo historical weather: bounded, rate-limited REST ingestion into immutable landing (no Spark).

Source: Open-Meteo Historical Weather API (archive-api.open-meteo.com), ERA5 reanalysis. The free
API is for non-commercial use (this portfolio project). Data licence CC BY 4.0: "Weather data by
Open-Meteo.com" (https://open-meteo.com/); ERA5 is from the Copernicus Climate Change Service.
Terms checked September 24, 2026: 600 calls/minute, 5,000/hour, 10,000/day, where one call is one
location, up to 10 variables and up to 14 days; longer requests count fractionally more (one year
of one location is ~26 calls).

Each response is landed byte-for-byte under a file name derived from its request and is never
overwritten: a rerun skips files that exist, so a backfill larger than one run's call budget
resumes where the previous run stopped. The request spec below is frozen for landing v1; changing
locations, variables or the model needs a new landing version (and a new pipeline append flow).
"""
from collections import deque
from dataclasses import dataclass
import datetime as dt
import hashlib
import io
import json
import os
from pathlib import Path
import re
import time
import urllib.error
import urllib.parse
import urllib.request

ENDPOINT = "https://archive-api.open-meteo.com/v1/archive"
ATTRIBUTION = "Weather data by Open-Meteo.com (CC BY 4.0), ERA5 from the Copernicus Climate Change Service"
LICENSE = "CC BY 4.0 (https://open-meteo.com/en/licence); free API for non-commercial use only"
LANDING_VERSION = "v1"
MODEL = "era5"  # One reanalysis for the whole window: best_match mixes models over time.
DAILY = ("temperature_2m_max", "temperature_2m_mean", "apparent_temperature_max")
TEMPERATURE_UNIT = "celsius"
USER_AGENT = "SentinelOps-portfolio/0.1 (non-commercial; Open-Meteo archive backfill)"
# Published free-tier limits (weighted calls) and the share of each this project allows itself.
# The limits are per IP address and serverless egress IPs are shared, so the margin matters.
LIMITS = {"minute": (60, 600), "hour": (3600, 5000), "day": (86400, 10000)}
SAFETY = 0.8
DAYS_PER_CALL, VARIABLES_PER_CALL = 14, 10


@dataclass(frozen=True)
class Location:
    """One representative point per state: the state's largest city (2020 Census)."""
    state: str  # As OSHA spells it, upper case.
    city: str
    latitude: float
    longitude: float
    timezone: str  # Daily values are aggregated over the local calendar day.

    @property
    def location_id(self) -> str:
        return f"{slug(self.state)}--{slug(self.city)}"


def slug(text: str) -> str:
    return re.sub(r"[^a-z]+", "-", text.lower()).strip("-")


# The 20 states with the most OSHA heat-illness reports (2015-01 to 2025-11), 92% of them, in
# that order. One city stands in for a whole state: a proxy, not a statewide average.
LOCATIONS_V1 = (
    Location("TEXAS", "Houston", 29.76, -95.37, "America/Chicago"),
    Location("FLORIDA", "Jacksonville", 30.33, -81.66, "America/New_York"),
    Location("GEORGIA", "Atlanta", 33.75, -84.39, "America/New_York"),
    Location("PENNSYLVANIA", "Philadelphia", 39.95, -75.17, "America/New_York"),
    Location("ARKANSAS", "Little Rock", 34.75, -92.29, "America/Chicago"),
    Location("MISSOURI", "Kansas City", 39.10, -94.58, "America/Chicago"),
    Location("LOUISIANA", "New Orleans", 29.95, -90.07, "America/Chicago"),
    Location("ILLINOIS", "Chicago", 41.88, -87.63, "America/Chicago"),
    Location("OKLAHOMA", "Oklahoma City", 35.47, -97.52, "America/Chicago"),
    Location("ALABAMA", "Huntsville", 34.73, -86.59, "America/Chicago"),
    Location("OHIO", "Columbus", 39.96, -83.00, "America/New_York"),
    Location("MISSISSIPPI", "Jackson", 32.30, -90.18, "America/Chicago"),
    Location("NEW JERSEY", "Newark", 40.74, -74.17, "America/New_York"),
    Location("KANSAS", "Wichita", 37.69, -97.33, "America/Chicago"),
    Location("NEW YORK", "New York", 40.71, -74.01, "America/New_York"),
    Location("WISCONSIN", "Milwaukee", 43.04, -87.91, "America/Chicago"),
    Location("MASSACHUSETTS", "Boston", 42.36, -71.06, "America/New_York"),
    Location("COLORADO", "Denver", 39.74, -104.99, "America/Denver"),
    Location("NEBRASKA", "Omaha", 41.26, -95.93, "America/Chicago"),
    Location("WEST VIRGINIA", "Charleston", 38.35, -81.63, "America/New_York"),
)
FILE_PATTERN = re.compile(r"^(?P<model>[a-z0-9]+)_(?P<state>[a-z-]+?)--(?P<city>[a-z-]+)"
                          r"_(?P<start>\d{4}-\d{2}-\d{2})_(?P<end>\d{4}-\d{2}-\d{2})\.json$")


def spec_digest() -> str:
    """Fingerprint of everything that shapes a v1 response; a test pins it."""
    spec = {"endpoint": ENDPOINT, "model": MODEL, "daily": DAILY, "temperature_unit": TEMPERATURE_UNIT,
            "locations": [vars(location) for location in LOCATIONS_V1]}
    return hashlib.sha256(json.dumps(spec, sort_keys=True).encode()).hexdigest()


def call_weight(days: int, variables: int = len(DAILY), locations: int = 1) -> float:
    """Open-Meteo's fractional call count: 1 per location, scaled up beyond 14 days or 10 variables."""
    return locations * max(1.0, days / DAYS_PER_CALL) * max(1.0, variables / VARIABLES_PER_CALL)


@dataclass(frozen=True)
class Request:
    location: Location
    start: dt.date
    end: dt.date

    @property
    def days(self) -> int:
        return (self.end - self.start).days + 1

    @property
    def weight(self) -> float:
        return call_weight(self.days)

    @property
    def file_name(self) -> str:
        return f"{MODEL}_{self.location.location_id}_{self.start.isoformat()}_{self.end.isoformat()}.json"

    @property
    def url(self) -> str:
        query = {"latitude": f"{self.location.latitude:.2f}", "longitude": f"{self.location.longitude:.2f}",
                 "start_date": self.start.isoformat(), "end_date": self.end.isoformat(),
                 "daily": ",".join(DAILY), "timezone": self.location.timezone, "models": MODEL,
                 "temperature_unit": TEMPERATURE_UNIT}
        return f"{ENDPOINT}?{urllib.parse.urlencode(query, safe=',')}"


def plan(first_year: int, last_year: int, locations=LOCATIONS_V1) -> list[Request]:
    """One request per location and calendar year, location-major: the most important states first."""
    if not 1940 <= first_year <= last_year:
        raise ValueError("Years must be ordered and within the archive (1940 onwards)")
    return [Request(location, dt.date(year, 1, 1), dt.date(year, 12, 31))
            for location in locations for year in range(first_year, last_year + 1)]


def parse_file_name(name: str) -> dict:
    match = FILE_PATTERN.fullmatch(name)
    if not match:
        raise ValueError(f"Not a landed Open-Meteo file name: {name}")
    return match.groupdict()


class RateLimited(RuntimeError):
    """HTTP 429: stop at once. Retrying would only extend the block."""


class Rejected(RuntimeError):
    """A response that must not be landed (client error, or not a JSON object)."""


def http_get(url: str, timeout: float = 60.0) -> tuple[int, bytes]:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.read()


def error_reason(body: bytes) -> str:
    try:
        return str(json.loads(body).get("reason", ""))[:200]
    except (ValueError, AttributeError):
        return body[:200].decode("utf-8", "replace")


def fetch(request: Request, get=http_get, sleep=time.sleep, retries: int = 2, backoff: float = 10.0) -> bytes:
    """The raw response body of one request. Retries only server errors and network failures."""
    for attempt in range(retries + 1):
        try:
            status, body = get(request.url)
        except (urllib.error.URLError, TimeoutError, ConnectionError) as error:
            status, body = None, str(error).encode()
        if status == 200:
            try:
                document = json.loads(body)
            except ValueError as error:
                raise Rejected(f"{request.file_name}: response is not JSON") from error
            if not isinstance(document, dict) or document.get("error"):
                raise Rejected(f"{request.file_name}: {error_reason(body)}")
            return body
        if status == 429:
            raise RateLimited(f"{request.file_name}: {error_reason(body)}")
        if status is not None and 400 <= status < 500:
            raise Rejected(f"{request.file_name}: HTTP {status} {error_reason(body)}")
        if attempt == retries:
            raise RuntimeError(f"{request.file_name}: HTTP {status} after {retries + 1} attempts: "
                               f"{error_reason(body)}")
        sleep(backoff * (attempt + 1))


class Pacer:
    """Keeps the weighted calls of any rolling minute under a limit by sleeping before a request."""

    def __init__(self, per_minute: float, clock=time.monotonic, sleep=time.sleep):
        self.per_minute, self.clock, self.sleep = per_minute, clock, sleep
        self.window = deque()

    def wait(self, weight: float) -> None:
        while True:
            now = self.clock()
            while self.window and now - self.window[0][0] >= 60:
                self.window.popleft()
            if not self.window or sum(w for _, w in self.window) + weight <= self.per_minute:
                self.window.append((now, weight))
                return
            self.sleep(60 - (now - self.window[0][0]) + 0.01)


class LocalStore:
    """A landing directory on a local disk (rehearsal and tests)."""

    def __init__(self, directory: Path):
        self.directory = Path(directory)

    def existing(self) -> set[str]:
        return {p.name for p in self.directory.glob("*.json")} if self.directory.exists() else set()

    def read(self, name: str) -> bytes:
        return (self.directory / name).read_bytes()

    def write_new(self, name: str, data: bytes) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        partial = self.directory / f".{name}.partial"
        partial.write_bytes(data)
        try:
            os.link(partial, self.directory / name)  # Fails if the file exists: never overwrites.
        finally:
            partial.unlink()


class VolumeStore:
    """A Unity Catalog volume directory through the Files API. Each upload is a single PUT with
    overwrite=False, so a file appears whole or not at all and is never replaced."""

    def __init__(self, client, directory: str):
        self.client, self.directory = client, directory.rstrip("/")

    def existing(self) -> set[str]:
        from databricks.sdk.errors import NotFound
        try:
            return {Path(entry.path).name for entry in self.client.files.list_directory_contents(self.directory)
                    if not entry.is_directory and entry.path.endswith(".json")}
        except NotFound:
            return set()

    def read(self, name: str) -> bytes:
        return self.client.files.download(f"{self.directory}/{name}").contents.read()

    def write_new(self, name: str, data: bytes) -> None:
        from databricks.sdk.errors import AlreadyExists, ResourceAlreadyExists
        self.client.files.create_directory(self.directory)  # Idempotent.
        try:
            self.client.files.upload(f"{self.directory}/{name}", io.BytesIO(data), overwrite=False)
        except (AlreadyExists, ResourceAlreadyExists) as error:
            raise FileExistsError(name) from error


def refuses_overwrite(store, name: str) -> bool:
    """Probe the store's guard: re-sending a landed file's own bytes must be refused. (If the guard
    were missing, the probe would rewrite identical bytes, and the caller fails the run.)"""
    try:
        store.write_new(name, store.read(name))
    except FileExistsError:
        return True
    return False


def used_calls(logs: list[dict], now: dt.datetime) -> dict:
    """Weighted calls this project already made in each rolling window, from earlier fetch logs.
    (The limits are per IP and serverless IPs change, so this is a self-imposed guardrail.)"""
    used = {window: 0.0 for window in LIMITS}
    for log in logs:
        for item in log.get("requests", []):
            age = (now - dt.datetime.fromisoformat(item["requested_at"])).total_seconds()
            for window, (seconds, _) in LIMITS.items():
                if 0 <= age < seconds:
                    used[window] += item["weight"]
    return used


def run(requests: list[Request], store, max_weighted_calls: float, previous_logs: list[dict] = (),
        get=http_get, clock=time.monotonic, sleep=time.sleep, now=lambda: dt.datetime.now(dt.timezone.utc)) -> dict:
    """Fetch and land every request whose file is missing, within this run's call budget.

    The budget is the smaller of --max-weighted-calls and what the hourly and daily limits (at
    SAFETY share) leave after earlier runs. Stops cleanly at the budget; a later run resumes.
    Stops with log["error"] on HTTP 429, a rejected response or repeated server errors; the caller
    saves the log (what was landed) and then fails.
    """
    started = now()
    used = used_calls(list(previous_logs), started)
    budget = min([max_weighted_calls] + [SAFETY * limit - used[w] for w, (_, limit) in LIMITS.items()
                                         if w != "minute"])
    present = store.existing()
    pending = [r for r in requests if r.file_name not in present]
    pacer = Pacer(SAFETY * LIMITS["minute"][1], clock=clock, sleep=sleep)
    log = {"source": ENDPOINT, "attribution": ATTRIBUTION, "license": LICENSE, "landing_version": LANDING_VERSION,
           "spec_sha256": spec_digest(), "started_at": started.isoformat(), "planned": len(requests),
           "already_present": len(requests) - len(pending), "used_before": used, "budget": round(budget, 2),
           "requests": [], "stopped": "complete"}
    spent = 0.0
    try:
        for request in pending:
            if spent + request.weight > budget:
                log["stopped"] = "budget"
                break
            pacer.wait(request.weight)
            requested_at = now()
            body = fetch(request, get=get, sleep=sleep)
            spent += request.weight
            item = {"file": request.file_name, "requested_at": requested_at.isoformat(),
                    "weight": round(request.weight, 4), "bytes": len(body),
                    "sha256": hashlib.sha256(body).hexdigest()}
            try:
                store.write_new(request.file_name, body)
            except FileExistsError:
                item["skipped"] = "appeared during the run"
            log["requests"].append(item)
    except (RateLimited, Rejected, RuntimeError) as error:
        log["stopped"], log["error"] = "error", f"{type(error).__name__}: {error}"
    landed = [item for item in log["requests"] if "skipped" not in item]
    log.update(finished_at=now().isoformat(), fetched=len(landed), bytes=sum(i["bytes"] for i in landed),
               weighted_calls=round(spent, 2), remaining=len(pending) - len(log["requests"]))
    return log


if __name__ == "__main__":
    # Local rehearsal: land one approved request into git-ignored data/.
    first = plan(2015, 2015)[0]
    print(json.dumps(run([first], LocalStore(Path("data/open_meteo/rehearsal/v1/daily")), max_weighted_calls=30),
                     indent=2))
