"""Scan assistant answers for employer names, locally, against the raw OSHA archive (never uploaded).

Usage: python scripts/scan_answer_names.py <answer-eval report JSON> [data/osha]

The report is the JSON line an `osha_answer_eval` run prints (or a docs/ evidence file with
`per_question`). Two checks per answer:
- cited: for each report the answer cites, the leading word sequences (2+ words, not all business
  words) of its employer name, or a distinctive single word (4+ letters, not a business or
  ordinary word, not a state), capitalized in the answer;
- any report: a capitalized two-word phrase in the answer that starts some employer's name in
  the whole archive (catches names from reports that weren't cited).
Phrases are excluded only when every word is a business word: the eval v2 leak was a business
word plus an ordinary word, which a "distinctive words only" rule missed.
Prints counts and question IDs only; names are never printed.
"""
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from sentinelops import osha  # noqa: E402

report = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
rows = report["per_question"]
raw = osha.read_reports(Path(sys.argv[2] if len(sys.argv) > 2 else "data/osha") / osha.ARCHIVE)
common = osha.common_words(raw["Final Narrative"])
employer = dict(zip(raw.UPA.astype("int64"), raw.Employer))


def plain(word: str) -> bool:
    w = word.lower()
    return w in osha.BUSINESS_WORDS or w in common or w in osha.US_STATES


def business(words) -> bool:
    return all(w.lower() in osha.BUSINESS_WORDS for w in words)


def capitalized(phrase: str, text: str) -> bool:
    parts = [re.escape(p[0].upper()) + "(?i:" + re.escape(p[1:]) + ")" for p in phrase.split()]
    return re.search(r"(?<!\w)" + r"\s+".join(parts) + r"(?!\w)", text) is not None


leading = set()
for name in raw.Employer:
    words = osha.NAME_WORD.findall(osha.SUFFIX.sub("", name))
    if len(words) >= 2 and not business(words[:2]):
        leading.add(f"{words[0]} {words[1]}".lower())

flagged = []
for row in rows:
    text = row.get("answer") or ""
    kinds = set()
    for rid in row.get("cited_ids") or []:
        words = osha.NAME_WORD.findall(osha.SUFFIX.sub("", str(employer.get(int(rid), ""))))
        phrases = [" ".join(words[:k]) for k in range(len(words), 1, -1) if not business(words[:k])]
        phrases += [w for w in words if len(w) >= 4 and not plain(w)]
        if any(capitalized(p, text) for p in phrases):
            kinds.add("cited_report_employer")
    for first, second in re.findall(r"(?<!\w)([A-Z][A-Za-z'&-]+)\s+([A-Z][A-Za-z'&-]+)", text):
        if f"{first} {second}".lower() in leading:
            kinds.add("any_report_employer_phrase")
    if kinds:
        flagged.append({"question_set": row.get("question_set"), "question_id": row["question_id"],
                        "status": row.get("status"), "kinds": sorted(kinds)})
print(json.dumps({"answers_scanned": len(rows), "answers_with_text": sum(bool(r.get("answer")) for r in rows),
                  "flagged": flagged}, indent=1))
