"""Code-grounded retrieval evaluation for the OSHA safety assistant.

Each question is a paraphrase that avoids OSHA's literal code titles where
practical. A document is relevant when its codes satisfy every clause of the
question's rule; a clause matches when any of its columns matches its
case-insensitive regex. Limits: OSHA codes are noisy, and every document's header
repeats its code titles, so absolute scores are optimistic. The comparison between
methods and dimensions is the point. Off-topic questions have no relevant reports;
their best scores inform a future abstention threshold.
"""
import hashlib
import json

import numpy as np
import pandas as pd

# v2: the table-saw rule also matches OSHA's reversed title "Stationary saws  table"
# (a vocabulary bug found by inspecting v1 results; it applies equally to every method).
VERSION = "osha-retrieval-v2"
PRESS = r"\bpress"
QUESTIONS = [
    {"id": "press_fingertip", "question": "A worker's fingertip was cut off by a mechanical power press.",
     "rule": [{"nature_title": "amput"}, {"body_part_title": "finger|thumb"},
              {"source_title": PRESS, "secondary_source_title": PRESS}]},
    {"id": "stepladder_fracture", "question": "An employee fell off a stepladder and broke a bone.",
     "rule": [{"event_title": "fall"}, {"source_title": "ladder"}, {"nature_title": "fractur"}]},
    {"id": "conveyor_caught", "question": "A worker's hand was pulled into a moving conveyor belt.",
     "rule": [{"source_title": "conveyor", "secondary_source_title": "conveyor"}, {"event_title": "caught|entangle|compress"}]},
    {"id": "forklift_foot", "question": "A forklift ran over a warehouse worker's foot.",
     "rule": [{"source_title": "forklift", "secondary_source_title": "forklift"}, {"body_part_title": "foot|feet|toe|ankle"}]},
    {"id": "heat_stroke", "question": "A landscaper collapsed from heat stroke while working outside on a hot day.",
     "rule": [{"event_title": "environmental heat"}]},
    {"id": "power_line", "question": "A lineman was electrocuted by an overhead power line.",
     "rule": [{"source_title": "power lines", "secondary_source_title": "power lines"}, {"event_title": "electric"}]},
    {"id": "fryer_burn", "question": "A cook was burned by hot oil from a deep fryer.",
     "rule": [{"source_title": "cooking grease|fryer", "secondary_source_title": "cooking grease|fryer"}, {"nature_title": "burn"}]},
    {"id": "skylight_fall", "question": "A roofer fell through a skylight.",
     "rule": [{"source_title": "skylight|roof opening"}, {"event_title": "fall"}]},
    {"id": "table_saw_amputation", "question": "A table saw cut off part of a carpenter's finger.",
     "rule": [{"source_title": r"table saw|saws\s+table"}, {"nature_title": "amput"}]},
    {"id": "trench_cave_in", "question": "A worker was buried when the walls of a trench caved in.",
     "rule": [{"event_title": "trenching cave-in", "source_title": "trenches, excavations"}]},
    {"id": "meat_grinder", "question": "An employee's hand was caught in a meat grinder at a processing plant.",
     "rule": [{"source_title": "butcher|meat|food slicer", "secondary_source_title": "butcher|meat|food slicer"}]},
    {"id": "eye_flying_fragment", "question": "A piece of metal flew into a worker's eye while grinding.",
     "rule": [{"body_part_title": r"\beye"}]},
    {"id": "chemical_burn", "question": "A worker's skin was burned by a splash of caustic chemical.",
     "rule": [{"nature_title": "chemical burn"}]},
    {"id": "tree_struck", "question": "A logger was struck by a falling tree.",
     "rule": [{"source_title": r"\btrees?\b|\blogs?\b|logging", "secondary_source_title": r"\btrees?\b|\blogs?\b|logging"},
              {"event_title": "struck"}]},
    {"id": "truck_dock_pinned", "question": "A worker was pinned between a truck and a loading dock.",
     "rule": [{"source_title": "dock", "secondary_source_title": "dock"}, {"event_title": "caught|compress|pinned|crush"}]},
    {"id": "slip_ankle_fracture", "question": "An employee slipped on an icy walkway and broke an ankle.",
     "rule": [{"event_title": "slip"}, {"body_part_title": "ankle"}, {"nature_title": "fractur"}]},
    {"id": "scaffold_fall", "question": "A painter fell from scaffolding.",
     "rule": [{"source_title": "scaffold"}, {"event_title": "fall"}]},
    {"id": "dog_bite", "question": "A delivery driver was attacked and bitten by a dog.",
     "rule": [{"source_title": "dog"}]},
    {"id": "toxic_inhalation", "question": "A worker was hospitalized after breathing in toxic fumes.",
     "rule": [{"event_title": "inhalation"}]},
    {"id": "crane_load", "question": "A load dropped from an overhead crane and struck a worker.",
     "rule": [{"source_title": "crane|hoist", "secondary_source_title": "crane|hoist"}, {"event_title": "struck"}]},
    {"id": "lifting_back", "question": "An employee injured their back lifting a heavy box.",
     "rule": [{"event_title": "overexertion"}, {"body_part_title": "back|spine|lumbar"}]},
    {"id": "explosion", "question": "A pressure tank exploded and injured nearby workers.",
     "rule": [{"event_title": "explosion"}]},
    {"id": "lawn_mower", "question": "A groundskeeper was injured by a riding lawn mower.",
     "rule": [{"source_title": "mower", "secondary_source_title": "mower"}]},
    {"id": "nail_gun", "question": "A nail gun shot a nail into a framer's hand.",
     "rule": [{"source_title": "nail", "secondary_source_title": "nail"}]},
    {"id": "drill_entangled", "question": "A worker's glove caught in a spinning drill press and pulled in the arm.",
     "rule": [{"source_title": r"\bdrills?\b|lathe", "secondary_source_title": r"\bdrills?\b|lathe"},
              {"event_title": "caught|entangle"}]},
    {"id": "toe_amputation", "question": "A worker lost a toe after a heavy object crushed the foot.",
     "rule": [{"nature_title": "amput"}, {"body_part_title": "toe"}]},
    {"id": "arc_flash", "question": "An electrician was burned by an arc flash in an electrical panel.",
     "rule": [{"event_title": "electric arc", "nature_title": "electrical burn"}]},
    {"id": "backing_vehicle", "question": "A worker on foot was hit by a vehicle that was backing up.",
     "rule": [{"event_title": "pedestrian struck"}]},
]
OFF_TOPIC = [
    {"id": "off_capital", "question": "What is the capital of France?"},
    {"id": "off_password", "question": "How do I reset my email password?"},
    {"id": "off_revenue", "question": "Explain how quarterly revenue is recognized under accounting rules."},
    {"id": "off_recipe", "question": "What is a good recipe for banana bread?"},
]
RULE_COLUMNS = {"nature_title", "body_part_title", "event_title", "source_title", "secondary_source_title"}


def fingerprint() -> str:
    payload = json.dumps({"version": VERSION, "questions": QUESTIONS, "off_topic": OFF_TOPIC}, sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()[:12]


def relevant(frame: pd.DataFrame, rule: list[dict]) -> np.ndarray:
    """Boolean mask of documents satisfying every clause (any column per clause)."""
    mask = np.ones(len(frame), dtype=bool)
    for clause in rule:
        if not clause or set(clause) - RULE_COLUMNS:
            raise ValueError(f"Invalid clause {clause}")
        hit = np.zeros(len(frame), dtype=bool)
        for column, pattern in clause.items():
            hit |= frame[column].fillna("").str.contains(pattern, case=False, regex=True).to_numpy()
        mask &= hit
    return mask


def ranking_metrics(ranked_ids, relevant_ids: set, k: int = 10) -> dict:
    """Precision@5/10, reciprocal rank and binary nDCG@k for one ranked list."""
    hits = np.array([doc in relevant_ids for doc in list(ranked_ids)[:k]], dtype=float)
    if len(hits) < k:
        raise ValueError(f"Need at least {k} ranked results")
    first = np.flatnonzero(hits)
    discounts = 1 / np.log2(np.arange(2, k + 2))
    ideal = discounts[:min(k, len(relevant_ids))].sum()
    return {"p_at_5": hits[:5].mean(), "p_at_10": hits.mean(), "mrr": 1 / (first[0] + 1) if len(first) else 0.0,
            "ndcg_at_10": float(hits @ discounts / ideal) if ideal else 0.0, "hit_at_1": hits[0]}


def relevance(frame: pd.DataFrame, questions=QUESTIONS) -> dict:
    """Question id -> (relevant report ids, base rate) over the evaluated corpus."""
    out = {}
    for question in questions:
        mask = relevant(frame, question["rule"])
        out[question["id"]] = (set(frame.report_id[mask].tolist()), float(mask.mean()))
    return out


def score(method: str, ranked_ids, top_scores, truth: dict, questions=QUESTIONS, k: int = 10) -> list[dict]:
    """Per-question metrics for one method's rankings (rows follow `questions`)."""
    rows = []
    for question, ids, scores in zip(questions, ranked_ids, top_scores):
        relevant_ids, base_rate = truth[question["id"]]
        metrics = ranking_metrics(ids, relevant_ids, k)
        rows.append({"method": method, "question_id": question["id"], "relevant": len(relevant_ids),
                     "base_rate": base_rate, **metrics, "lift_at_10": metrics["p_at_10"] / base_rate,
                     "top1_score": float(scores[0]), "top_ids": [int(i) for i in ids[:k]]})
    return rows


def summarize(rows: pd.DataFrame) -> pd.DataFrame:
    """Mean metrics per method over on-topic questions."""
    metrics = ["p_at_5", "p_at_10", "mrr", "ndcg_at_10", "hit_at_1", "lift_at_10", "top1_score"]
    return rows.groupby("method")[metrics].mean().round(4)
