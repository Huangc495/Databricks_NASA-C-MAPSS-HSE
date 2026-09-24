import json

import numpy as np
import pandas as pd
import pytest

from sentinelops import extraction as x


def reports(rows):
    """Minimal Gold-shaped frame: (id, month, event code/title, body code/title, source code/title, nature title)."""
    return pd.DataFrame(rows, columns=["report_id", "event_month", "event_code", "event_title", "body_part_code",
                                       "body_part_title", "source_code", "source_title", "nature_title"])


def test_split_is_deterministic_and_roughly_one_percent_test():
    splits = pd.Series([x.split(i) for i in range(20000)])
    assert splits.tolist() == [x.split(i) for i in range(20000)]
    shares = splits.value_counts(normalize=True)
    assert 0.007 < shares["test"] < 0.013 and 0.0005 < shares["dev"] < 0.002
    assert x.split(np.int64(931176)) == x.split(931176)


def test_nature_titles_map_by_priority_and_nonspecific_titles_are_unscored():
    assert x.nature_type("Amputations, avulsions, enucleations  unspecified") == "amputation"
    assert x.nature_type("Fractures and burns") == "fracture"
    assert x.nature_type("Heat (thermal) burns, unspecified") == "burn"
    assert x.nature_type("Heat exhaustion, prostration") == "heat_illness"
    assert x.nature_type("Cuts, lacerations") == "cut_or_puncture"
    assert x.nature_type("Myocardial infarction (heart attack)") == "other"
    for vague in ("Soreness, pain, hurt-nonspecified injury", "Traumatic injuries and disorders, unspecified", None):
        assert x.nature_type(vague) is None


def test_truth_harmonizes_to_the_current_scheme():
    frame = reports([
        (1, "2019-05", "6411", "Caught in running equipment", "31", "Hip(s)", "8621", "Forklift", "Fractures"),
        (2, "2024-03", "6411", "Caught in running equipment", "51", "Hip joint(s)", "8621", "Forklift", "Fractures"),
        (3, "2018-01", "11", "Struck by animal, unspecified", "8", "Trunk and hip joint(s)", "9111", "Scrap metal", "Cuts"),
        (4, "2025-01", "624", "Struck by animal  unspecified", "4421", "Fingertip(s)", "4411", "Scrap metal", "Burns"),
        (5, "2016-07", "9999", "Nonclassifiable", "9999", "Nonclassifiable", "9999", "Nonclassifiable",
         "Soreness, pain, hurt-nonspecified injury"),
    ])
    truth, changed = x.truth(frame)
    assert truth.era.tolist() == ["2015-23", "2024-25", "2015-23", "2024-25", "2015-23"]
    assert truth.body_part.tolist()[:4] == ["lower_extremities", "lower_extremities", "multiple_body_parts",
                                            "upper_extremities"]
    # Titles also seen in 2024-25 take their current division: animal strikes and scrap metal moved.
    assert truth.event[2] == "contact_with_object" and truth.source[2] == "parts_and_materials"
    assert truth.loc[4, ["event", "body_part", "source", "nature"]].isna().all()
    assert changed == {"event": 1, "body_part": 1, "source": 1}


def test_prompt_and_schema_offer_exactly_the_labels():
    properties = x.SCHEMA["json_schema"]["schema"]["properties"]
    for field in x.FIELDS:
        assert properties[field]["enum"] == x.labels(field)
        assert all(f"- {label}:" in x.PROMPT for label in x.labels(field) if label != "unspecified")
    assert x.SCHEMA["json_schema"]["strict"] and "'" not in json.dumps(x.SCHEMA)
    assert x.request("  A worker fell.  ").endswith("Narrative:\nA worker fell.")


def test_parse_validates_every_field_in_code():
    good = {"event": "fall_slip_trip", "nature": "fracture", "body_part": "lower_extremities",
            "source_object": "step ladder", "source": "tools_instruments_equipment"}
    parsed, error = x.parse(json.dumps(good))
    assert error is None and parsed == good
    assert x.parse(json.dumps({**good, "source": "ladders"}))[1].startswith("source=")
    assert x.parse("not json")[1].startswith("invalid JSON") and x.parse(None)[1] == "empty response"
    assert x.parse("[1, 2]")[1] == "not a JSON object"


def test_scores_count_invalid_predictions_as_wrong_and_skip_unscoreable_truth():
    truth = pd.DataFrame({"report_id": [1, 2, 3, 4], "era": ["2015-23", "2015-23", "2024-25", "2024-25"],
                          "event": ["fall_slip_trip", "transportation", "fall_slip_trip", None],
                          "nature": ["fracture"] * 4, "body_part": ["head"] * 4, "source": ["vehicles"] * 4})
    predicted = pd.DataFrame({"report_id": [1, 2, 3, 4], "event": ["fall_slip_trip", "contact_with_object", None,
                                                                    "overexertion"],
                              "nature": ["fracture"] * 4, "body_part": ["head"] * 4, "source": ["vehicles"] * 4})
    event = x.score(truth, predicted)["event"]
    assert event["scored"] == 3 and event["accuracy"] == pytest.approx(1 / 3)
    assert event["accuracy_by_era"] == {"2015-23": 0.5, "2024-25": 0.0}
    assert x.correct(truth, predicted, "event").tolist() == [True, False, False]
    assert x.majority(truth) == {"event": "fall_slip_trip", "nature": "fracture", "body_part": "head",
                                 "source": "vehicles"}


def test_paired_bootstrap_difference_and_interval():
    a = pd.Series([True, True, True, False], index=[1, 2, 3, 4])
    b = pd.Series([True, False, False, False], index=[1, 2, 3, 4])
    result = x.paired_bootstrap(a, b, samples=500)
    assert result["difference"] == 0.5 and result["reports"] == 4
    assert result["ci95"][0] <= 0.5 <= result["ci95"][1]
