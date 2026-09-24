"""Verified OSHA Severe Injury Reports acquisition and data minimization (no cloud compute).

Source: U.S. Department of Labor, OSHA Severe Injury Reports, federal jurisdiction,
January 2015 - November 2025. Federal government work: public domain; DOL requests
attribution and prohibits implying endorsement. Employer identities, street addresses,
city, ZIP, coordinates and inspection numbers are dropped before landing, and
employer names/addresses inside narratives are masked. The raw archive stays local.
"""
import hashlib
import io
import json
from pathlib import Path
import re
import urllib.request
import zipfile

import pandas as pd

URL = "https://www.osha.gov/sites/default/files/January2015toNovember2025.zip"
SHA256 = "a3f7f434e200fb956131f12277378e592993a25db3f328716fbece106f846bb0"
ARCHIVE = "January2015toNovember2025.zip"
MEMBER = "January2015toNovember2025.csv"
ATTRIBUTION = "U.S. Department of Labor, Occupational Safety and Health Administration (osha.gov)"
DROPPED = ["Employer", "Address1", "Address2", "City", "Zip", "Latitude", "Longitude", "Inspection", "EventDate"]
CODES = {"Nature": "nature", "Part of Body": "body_part", "Event": "event", "Source": "source",
         "Secondary Source": "secondary_source"}
TITLES = {"Nature": "NatureTitle", "Part of Body": "Part of Body Title", "Event": "EventTitle",
          "Source": "SourceTitle", "Secondary Source": "Secondary Source Title"}
SUFFIX = re.compile(r"(?:[\s,]+(?:inc|llc|l\.l\.c|corp|corporation|co|company|ltd|lp|llp|pllc|pc|dba)\.?)+$"
                    r"|\s*#\s*\d+$", re.IGNORECASE)
STREET = re.compile(r"\b\d{1,6}(?:\s+(?:[A-Z][A-Za-z]*\.?|\d+(?:st|nd|rd|th)?)){1,4}\s+(?:Street|St|Avenue|Ave"
                    r"|Road|Rd|Drive|Dr|Boulevard|Blvd|Lane|Ln|Highway|Hwy|Parkway|Pkwy|Court|Ct|Way|Place|Pl)\b\.?")
ZIP = re.compile(r"\b([A-Z]{2}),?\s+\d{5}(?:-\d{4})?\b")


def download(destination: Path) -> Path:
    destination.mkdir(parents=True, exist_ok=True)
    archive = destination / ARCHIVE
    if not archive.exists():
        request = urllib.request.Request(URL, headers={"User-Agent": "Mozilla/5.0 SentinelOps/0.1"})
        with urllib.request.urlopen(request, timeout=300) as response:
            payload = response.read()
        if hashlib.sha256(payload).hexdigest() != SHA256:
            raise ValueError("OSHA archive checksum mismatch")
        archive.write_bytes(payload)
    if hashlib.sha256(archive.read_bytes()).hexdigest() != SHA256:
        raise ValueError("Cached OSHA archive checksum mismatch")
    return archive


def read_reports(archive: Path) -> pd.DataFrame:
    with zipfile.ZipFile(archive) as zipped:
        if zipped.namelist() != [MEMBER]:
            raise ValueError("Unexpected OSHA archive members")
        return pd.read_csv(io.BytesIO(zipped.read(MEMBER)), dtype=str, keep_default_na=False, encoding="utf-8")


def _literal(text: str, value: str, token: str) -> tuple[str, int]:
    value = " ".join(value.split())
    if len(value) < 5:  # Too short to mask without clobbering ordinary words.
        return text, 0
    pattern = r"(?<!\w)" + r"\s+".join(re.escape(part) for part in value.split()) + r"(?!\w)"
    return re.subn(pattern, token, text, flags=re.IGNORECASE)


def mask(narrative: str, employer: str, addresses: list[str]) -> tuple[str, int]:
    """Mask the report's employer and addresses, street addresses and state+ZIP; normalize whitespace."""
    text, total = " ".join(narrative.split()), 0
    core = SUFFIX.sub("", employer.strip())
    for value, token in [(employer, "[EMPLOYER]"), (core, "[EMPLOYER]"), *[(a, "[ADDRESS]") for a in addresses]]:
        text, count = _literal(text, value, token)
        total += count
    text, streets = STREET.subn("[ADDRESS]", text)
    text, zips = ZIP.subn(r"\1 [ZIP]", text)
    return text, total + streets + zips


def _count(values: pd.Series) -> pd.Series:
    """Whole, non-negative counts; blank means unknown and stays null."""
    values = values.str.strip()
    numbers = pd.to_numeric(values.where(values != ""), errors="raise")
    known = numbers.dropna()
    if (known < 0).any() or (known % 1 != 0).any():
        raise ValueError("Severity counts must be non-negative whole numbers")
    return pd.Series([None if pd.isna(n) else int(n) for n in numbers], index=values.index, dtype=object)


def _blank_to_none(values: pd.Series) -> pd.Series:
    values = values.str.strip().astype(object)
    return values.where(values != "", None)


def minimize(reports: pd.DataFrame) -> tuple[pd.DataFrame, dict]:
    """Keep only fields the assistant and analytics need; one row per unique UPA."""
    if reports.UPA.duplicated().any() or not reports.UPA.str.fullmatch(r"\d+").all():
        raise ValueError("UPA must be a unique numeric report key")
    dates = pd.to_datetime(reports.EventDate, format="%m/%d/%Y", errors="raise")
    masked = [mask(n, e, [a1, a2]) for n, e, a1, a2 in
              zip(reports["Final Narrative"], reports.Employer, reports.Address1, reports.Address2)]
    out = pd.DataFrame({
        "report_id": reports.UPA.astype("int64"),
        "osha_id": reports.ID,
        "event_month": dates.dt.strftime("%Y-%m"),
        "state": reports.State.str.strip(),
        "naics": reports["Primary NAICS"].str.strip(),
        "federal_state": reports.FederalState.astype("int64"),
        "hospitalized": _count(reports.Hospitalized),
        "amputation": _count(reports.Amputation),
        "loss_of_eye": _count(reports["Loss of Eye"]),
        "inspected": reports.Inspection.str.strip() != "",
        "narrative": [text for text, _ in masked],
    })
    for column, name in CODES.items():
        out[f"{name}_code"] = _blank_to_none(reports[column])
        out[f"{name}_title"] = _blank_to_none(reports[TITLES[column]])
    stats = {"rows": len(out), "masked_narratives": sum(1 for _, n in masked if n),
             "mask_replacements": sum(n for _, n in masked), "dropped_columns": DROPPED}
    return out, stats


def prepare(source: Path, destination: Path) -> dict:
    """Write immutable, per-year minimized JSONL files plus a SHA-256 manifest."""
    reports, stats = minimize(read_reports(download(source)))
    files = {}
    for year, part in reports.groupby(reports.event_month.str[:4], sort=True):
        lines = (json.dumps(record, sort_keys=True, ensure_ascii=False) for record in part.to_dict("records"))
        files[f"reports/sir_{year}.jsonl"] = ("\n".join(lines) + "\n").encode("utf-8")
    manifest = {"source": URL, "archive_sha256": SHA256, "member": MEMBER, "attribution": ATTRIBUTION,
                "license": "U.S. federal government work (public domain); attribution requested; no endorsement implied",
                "coverage": "Severe injury reports (hospitalization, amputation, loss of an eye) with event dates "
                            "2015-01-01 to 2025-11-30 as published; OSHA's page says State Plan reports are "
                            "excluded from its dashboard dataset. FederalState flag kept as published.",
                **stats, "files": {}}
    for name, payload in files.items():
        path = destination / name
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and path.read_bytes() != payload:
            raise ValueError(f"Refusing to overwrite immutable landing file: {name}")
        path.write_bytes(payload)
        manifest["files"][name] = hashlib.sha256(payload).hexdigest()
    (destination / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return manifest


if __name__ == "__main__":
    print(json.dumps({k: v for k, v in prepare(Path("data/osha"), Path("data/landing/osha_v1")).items()
                      if k != "files"}, indent=2))
