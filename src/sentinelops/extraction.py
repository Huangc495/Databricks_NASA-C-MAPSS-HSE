"""Structured extraction of OSHA injury codes from narratives, scored against OSHA's coding.

The model reads only the narrative (the Gold document header repeats the code titles) and
returns event, nature, body part and source as JSON constrained by a schema.

Labels are chosen to be comparable across OSHA's 2024 coding change (codes and titles changed
from 2024; e.g. "Fractures" is 111 before and 124 after):
- event, body part and source: the OIICS division (first code digit), whose shares are stable
  across 2015-2025. Truth is harmonized to the current scheme: a title that also appears in
  2024-25 reports takes its 2024-25 division, and hips count as lower extremities (they moved
  from trunk).
- nature: the division is uninformative (99.6% "traumatic injuries"), so titles are mapped by
  ordered rules to injury types (amputation before fracture before burn ...).
Truth that no narrative can recover (nonclassifiable, unspecified) is excluded from scoring.
"""
import hashlib
import json
import re

import numpy as np
import pandas as pd

VERSION = "osha-extraction-v1"
# Tuned only on the dev split: v2 added OSHA source conventions (trips -> floor, machinery vs
# vehicles) and vehicle-involved transportation events after v1 dev errors (source 0.71). v3
# states the source rules before source_object, since v2 named the object tripped over first
# and then categorized it (source 0.72).
PROMPT_VERSION = "osha-extraction-prompt-v3"
FIELDS = ("event", "nature", "body_part", "source")
SPLIT_SALT = "osha-extraction-v1"
CURRENT_SCHEME_FROM = "2024-01"

# label -> (OIICS division, definition shown to the model). Examples follow OSHA's own coding.
EVENT = {
    "violence_or_animal": ("1", "Violence and other injuries by persons or animals: assaults, shootings, stabbings, "
                                "self-inflicted injuries, animal and insect bites, stings and attacks."),
    "transportation": ("2", "Transportation incident: a vehicle or mobile equipment in transport is involved, "
                            "including forklifts, pallet jacks, trucks, aircraft, boats and animals being ridden. "
                            "Covers collisions, rollovers, pedestrians struck by a vehicle, a worker caught between a "
                            "vehicle and another object, and falls or jumps from a vehicle. A forklift, pallet jack, "
                            "loader or other vehicle striking, running over or pinning a worker is transportation, "
                            "even at low speed in a warehouse or yard."),
    "fire_or_explosion": ("3", "Fire or explosion: ignition of vapors, gases, liquids or clothing, flash fires, "
                               "explosions of pressure vessels, piping or tires."),
    "fall_slip_trip": ("4", "Fall, slip or trip: falls to a lower level (from ladders, roofs, scaffolds, docks), falls "
                            "on the same level, slips and trips, jumps to a lower level."),
    "harmful_exposure": ("5", "Exposure to harmful substances or environments: electricity, environmental heat or "
                              "cold, contact with hot objects, liquids or steam, inhaling, swallowing or skin or eye "
                              "contact with harmful substances, oxygen deficiency, traumatic stress."),
    "contact_with_object": ("6", "Contact with objects and equipment: struck by falling, flying, swinging or rolling "
                                 "objects; struck against objects; caught in or compressed by machinery, equipment or "
                                 "objects; injured by a tool or object held by the worker."),
    "overexertion": ("7", "Overexertion and bodily reaction: lifting, pushing, pulling, carrying, repetitive motion, "
                          "bending, reaching, twisting or climbing without a fall."),
}
BODY_PART = {
    "head": ("1", "Head: skull, brain, face, eyes, ears, nose, mouth, teeth, scalp."),
    "neck": ("2", "Neck, including the throat."),
    "trunk": ("3", "Trunk: chest, ribs, back and spine, abdomen, pelvis, buttocks, groin, internal organs."),
    "upper_extremities": ("4", "Upper extremities: shoulders, collarbones, arms, elbows, wrists, hands, fingers."),
    "lower_extremities": ("5", "Lower extremities: hips, legs, thighs, knees, ankles, feet, toes."),
    "body_systems": ("6", "Body systems: effects on the whole body rather than a location, such as heat illness, "
                          "poisoning, electric shock, heart attack or breathing problems."),
    "multiple_body_parts": ("8", "Multiple body parts from different groups above, such as head and arm, or trunk "
                                 "and legs."),
}
SOURCE = {
    "chemicals": ("1", "Chemicals and chemical products: acids, caustics, cleaning agents, carbon monoxide, "
                       "ammonia, propane and other gases, fuels, solvents."),
    "containers_furniture_fixtures": ("2", "Containers, furniture and fixtures: boxes, crates, pallets, barrels, "
                                           "tanks, bins, bags, reels and rolls, shelving, furniture."),
    "machinery": ("3", "Machinery: presses, stationary saws (table, chop and miter saws), conveyors, mixers, meat "
                       "grinders and slicers, balers, compactors, ovens and ranges, cranes, hoists, aerial lifts and "
                       "elevators, and construction, agricultural and mining machinery such as skid steers, "
                       "front-end loaders, backhoes and excavators."),
    "parts_and_materials": ("4", "Parts and materials: building materials, lumber, pipes, metal stock, nails, machine "
                                 "and vehicle parts, trailers without their truck, power lines and electrical wiring, "
                                 "hot or molten metal, "
                                 "steam, scrap and debris."),
    "persons_plants_animals_minerals": ("5", "Persons, plants, animals and minerals: other people (co-workers, "
                                             "patients, assailants), the worker's own bodily motion, trees and "
                                             "logs, animals, food products and cooking oils, soil, rocks."),
    "structures_and_surfaces": ("6", "Structures and surfaces: floors, ground, walkways, stairs, doors, roofs, "
                                     "skylights, scaffolds, platforms, trenches and excavations, loading docks."),
    "tools_instruments_equipment": ("7", "Tools, instruments and equipment: ladders, hand tools, knives, powered hand "
                                         "tools, chainsaws and other hand-held saws, hand grinders, nail guns, welding "
                                         "torches."),
    "vehicles": ("8", "Vehicles: forklifts, stand-up lifts and order pickers, powered pallet jacks, trucks, "
                      "tractor-trailers, cars, buses, golf carts, aircraft, boats, trains."),
    "other_sources": ("9", "Other sources: environmental heat or cold, weather, water, and fire or flames not from "
                           "another listed source."),
}
# Nature types, in priority order: the first matching rule wins for OSHA titles, and the model
# is told to pick the first type that applies when several injuries are described.
NATURE = {
    "amputation": (r"amputat|avulsion|enucleat", "Amputation, including fingertips, avulsion or loss of an eye."),
    "fracture": (r"fractur", "Fracture (broken bone)."),
    "burn": (r"burn|corrosion", "Burn: thermal, chemical or electrical."),
    "electric_shock": (r"electrocution|electric shock", "Electric shock without a burn."),
    "heat_illness": (r"heat exhaustion|heat stroke|heat syncope|heat cramp|heat fatigue|effects of heat|hyperthermia"
                     r"|heat and light", "Heat illness: heat exhaustion, heat stroke, dehydration from heat."),
    "head_brain_injury": (r"intracranial|concussion|brain", "Concussion or other brain injury, bleeding in the skull."),
    "cut_or_puncture": (r"\bcut|laceration|puncture|gunshot|open wound|\bwounds?\b",
                        "Cut, laceration, puncture or gunshot wound."),
    "crushing": (r"crush", "Crushing injury."),
    "internal_injury": (r"internal|closed trauma", "Internal injury to organs or blood vessels."),
    "poisoning_or_respiratory": (r"poison|toxic|asphyx|respirat|inhal",
                                 "Poisoning, toxic effects or breathing injury from a substance."),
    "sprain_strain_or_bruise": (r"sprain|strain|\btears?\b|dislocat|hernia|herniat|bruise|contusion|swelling|abrasion",
                                "Sprain, strain, tear, dislocation, hernia, bruise or abrasion."),
    "other": (r"$^", "A specific injury not listed above, such as a heart attack or frostbite."),
}
# Nonspecific nature titles (including "Soreness, pain, hurt-nonspecified injury") say nothing a
# narrative could be scored on; the model may also answer "unspecified" when the narrative is silent.
NATURE_UNSPECIFIED = r"unspecified|n\.e\.c\.$|nonclassifiable|soreness|pain|nonspecified|multiple symptoms"
DIVISIONS = {"event": EVENT, "body_part": BODY_PART, "source": SOURCE}
UNSCOREABLE_TITLE = {"event": r"^nonclassifiable$|^event or exposure\b", "body_part": r"^nonclassifiable$|^part of body\b",
                     "source": r"^nonclassifiable$|^source\b"}


def labels(field: str) -> list[str]:
    """Allowed output labels; nature also allows 'unspecified' when the narrative doesn't say."""
    return list(NATURE) + ["unspecified"] if field == "nature" else list(DIVISIONS[field])


def split(report_id) -> str:
    """Deterministic ~1% test, ~0.1% dev, rest train, from a salted hash of the report ID."""
    bucket = int(hashlib.sha256(f"{SPLIT_SALT}:{int(report_id)}".encode()).hexdigest()[:8], 16) % 1000
    return "test" if bucket < 10 else "dev" if bucket < 11 else "train"


def _normalize(title) -> str:
    return re.sub(r"[^a-z0-9.]+", " ", str(title or "").lower()).strip()


def nature_type(title) -> str | None:
    """Injury type for an OSHA nature title; None when the title is nonspecific (not scored)."""
    text = str(title or "").lower().strip()
    if not text:
        return None
    for name, (pattern, _) in NATURE.items():
        if re.search(pattern, text):
            return name
    return None if re.search(NATURE_UNSPECIFIED, text) else "other"


def truth(frame: pd.DataFrame) -> tuple[pd.DataFrame, dict]:
    """Harmonized truth labels per report (None = not scoreable) and counts of harmonized changes.

    `frame` needs report_id, event_month and <field>_code / <field>_title for event, body_part,
    source, plus nature_title.
    """
    out = pd.DataFrame({"report_id": frame.report_id.to_numpy(),
                        "era": np.where(frame.event_month.to_numpy() >= CURRENT_SCHEME_FROM, "2024-25", "2015-23")})
    changed = {}
    current = frame.event_month.to_numpy() >= CURRENT_SCHEME_FROM
    for field, options in DIVISIONS.items():
        code_division = frame[f"{field}_code"].astype(str).str[0].to_numpy()
        title = frame[f"{field}_title"].map(_normalize).to_numpy()
        latest = pd.DataFrame({"title": title[current], "division": code_division[current]}) \
            .groupby("title").division.agg(lambda s: s.value_counts().index[0])
        division = pd.Series(title).map(latest).fillna(pd.Series(code_division)).to_numpy()
        if field == "body_part":
            hips = np.array([bool(re.search(r"\bhips?\b", t)) and "trunk" not in t for t in title])
            division = np.where(hips, "5", division)
        changed[field] = int((division != code_division).sum())
        by_division = {division_code: name for name, (division_code, _) in options.items()}
        unscoreable = np.array([bool(re.search(UNSCOREABLE_TITLE[field], t)) for t in title])
        out[field] = [None if skip else by_division.get(d) for d, skip in zip(division, unscoreable)]
    out["nature"] = frame.nature_title.map(nature_type).to_numpy()
    return out, changed


def _definitions(options: dict) -> str:
    return "\n".join(f"- {name}: {definition}" for name, (_, definition) in options.items())


PROMPT = f"""You code U.S. workplace injury reports the way OSHA injury coders do. Read the narrative and return JSON with:
- event: how the injury happened, one of:
{_definitions(EVENT)}
- nature: the injury type. If several injuries are described, choose the first type in this list that applies; use "unspecified" only if the narrative doesn't say what the injury was:
{_definitions(NATURE)}
- body_part: the part of the body injured:
{_definitions(BODY_PART)}
- source_object: in a few words, the source of the injury as OSHA codes it: the object, substance, surface, person or motion that directly produced the injury, following these rules:
  - A fall on the same level, including a trip over an object: the floor, walkway or ground, never the object tripped over.
  - A fall to a lower level: what the worker fell from (ladder, scaffold, roof, vehicle), not the surface landed on.
  - A vehicle or machine that moves, drops or pushes something into the worker: the vehicle or machine.
  - Overexertion: the object handled, or the worker's bodily motion.
- source: the category of source_object, one of:
{_definitions(SOURCE)}

Narrative:
"""
SCHEMA = {"type": "json_schema", "json_schema": {"name": "osha_injury_coding", "strict": True, "schema": {
    "type": "object",
    "properties": {"event": {"type": "string", "enum": labels("event")},
                   "nature": {"type": "string", "enum": labels("nature")},
                   "body_part": {"type": "string", "enum": labels("body_part")},
                   "source_object": {"type": "string"},
                   "source": {"type": "string", "enum": labels("source")}},
    "required": ["event", "nature", "body_part", "source_object", "source"],
    "additionalProperties": False}}}


def request(narrative: str) -> str:
    """The single user message sent to the model (identical for REST and ai_query)."""
    return PROMPT + str(narrative).strip()


def parse(text) -> tuple[dict | None, str | None]:
    """Validated extraction from the model's JSON text, or (None, reason). Never trusts the schema alone."""
    if not text:
        return None, "empty response"
    try:
        value = json.loads(text)
    except (TypeError, json.JSONDecodeError) as error:
        return None, f"invalid JSON: {str(error)[:80]}"
    if not isinstance(value, dict):
        return None, "not a JSON object"
    for field in FIELDS:
        if value.get(field) not in labels(field):
            return None, f"{field}={value.get(field)!r} is not an allowed label"
    return {**{field: value[field] for field in FIELDS}, "source_object": str(value.get("source_object", ""))[:200]}, None


def score(truth_frame: pd.DataFrame, predicted: pd.DataFrame) -> dict:
    """Per-field accuracy and macro-F1 on scoreable truth; unparsed predictions count as wrong.

    `predicted` has report_id and the four fields (None where the output was invalid).
    """
    from sklearn.metrics import f1_score
    merged = truth_frame.merge(predicted, on="report_id", suffixes=("", "_pred"))
    out = {}
    for field in FIELDS:
        rows = merged[merged[field].notna()]
        hit = (rows[field] == rows[f"{field}_pred"]).to_numpy()
        predicted_labels = rows[f"{field}_pred"].fillna("invalid")
        out[field] = {"scored": int(len(rows)), "accuracy": float(hit.mean()) if len(rows) else None,
                      "macro_f1": float(f1_score(rows[field], predicted_labels, average="macro", zero_division=0)),
                      "accuracy_by_era": {era: float(hit[(rows.era == era).to_numpy()].mean())
                                          for era in sorted(rows.era.unique())}}
    return out


def correct(truth_frame: pd.DataFrame, predicted: pd.DataFrame, field: str) -> pd.Series:
    """Per-report correctness for one field (scoreable truth only), indexed by report_id."""
    merged = truth_frame[truth_frame[field].notna()].merge(predicted, on="report_id", suffixes=("", "_pred"))
    return pd.Series((merged[field] == merged[f"{field}_pred"]).to_numpy(), index=merged.report_id)


def paired_bootstrap(a: pd.Series, b: pd.Series, samples: int = 2000, seed: int = 0) -> dict:
    """Mean accuracy difference a - b over the same reports, with a 95% percentile interval."""
    joined = pd.concat([a.rename("a"), b.rename("b")], axis=1, join="inner").astype(float)
    diff = (joined.a - joined.b).to_numpy()
    draws = np.random.default_rng(seed).integers(0, len(diff), size=(samples, len(diff)))
    means = diff[draws].mean(axis=1)
    return {"difference": float(diff.mean()), "ci95": [float(np.percentile(means, 2.5)), float(np.percentile(means, 97.5))],
            "reports": int(len(diff))}


def majority(train_truth: pd.DataFrame) -> dict:
    """Most frequent training label per field."""
    return {field: train_truth[field].dropna().value_counts().index[0] for field in FIELDS}


def fit_supervised(texts, targets):
    """TF-IDF + logistic regression with library defaults (no tuning), one model per field."""
    from sklearn.feature_extraction.text import TfidfVectorizer
    from sklearn.linear_model import LogisticRegression
    from sklearn.pipeline import make_pipeline
    return make_pipeline(TfidfVectorizer(sublinear_tf=True, ngram_range=(1, 2), min_df=3, dtype=np.float32),
                         LogisticRegression(max_iter=1000)).fit(texts, targets)
