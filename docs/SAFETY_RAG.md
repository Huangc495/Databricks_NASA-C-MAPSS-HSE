# Safety GenAI assistant: sources, privacy, retrieval and grounded answers

Goal: answer safety questions ("What typically causes amputations on press brakes?")
from OSHA Severe Injury Report narratives. Answers must cite report IDs and say
so when the reports don't support an answer. Status and run evidence are in
[STATUS.md](STATUS.md).

## Source and provenance

| Item | Value |
|---|---|
| Publisher | U.S. Department of Labor, OSHA: [Severe Injury Reports](https://www.osha.gov/severe-injury-reports) |
| File | `January2015toNovember2025.zip`, 16,224,511 bytes, `Last-Modified` 7 Aug 2026 |
| SHA-256 | `a3f7f434e200fb956131f12277378e592993a25db3f328716fbece106f846bb0` (pinned in `sentinelops.osha`) |
| Content | One UTF-8 CSV (57.4 MB); 105,996 reports; event dates 2015-01-01 to 2025-11-30 |
| License | Federal government work, generally public domain. DOL [asks](https://www.dol.gov/general/aboutdol/copyright) for credit to the U.S. Department of Labor and forbids implying endorsement. |
| Coverage caveat | Severe injuries only (hospitalization, amputation, loss of an eye). OSHA's page says State Plan reports are excluded from its dashboard dataset; the `FederalState` flag (2,492 rows = 0) is kept as published. |

`ID` is **not** unique: 5 IDs each cover two different incidents. `UPA` is
unique and becomes `report_id`, which is also the citation key.

## Privacy: minimize before anything leaves this machine

`python -m sentinelops.osha` validates the checksum and writes per-year JSONL
files plus a SHA-256 manifest under `data/landing/osha_v1`. The raw archive
stays local and git-ignored.

- **Dropped:** employer name, both address lines, city, ZIP, latitude/longitude,
  and the inspection number (it links to public records that name the
  employer). An `inspected` boolean replaces the number.
- **Coarsened:** the event date becomes the event month.
- **Masked in narratives:** each report's own employer name (full, and with
  suffixes like "Inc." or "#1234" removed) and its address lines, plus
  numbered-street patterns and state+ZIP. 282 narratives were masked (300
  replacements). A scan found no emails, phone numbers, SSN patterns or
  honorific names. Capitalized first-name pairs matched brands and places, not
  people.
- **Residual risk (documented, not masked):** narratives can still contain
  exact dates, cities, hospital names and other companies' names (for example,
  contractors). The assistant must not be used to identify individuals or
  employers.
- **Blank severity counts** (7 amputation, 5 loss of eye) stay null (unknown),
  not zero.

## Pipeline (`osha_safety`, job `osha_ingest`)

- **Bronze `osha_sir_reports`:** Auto Loader reads the JSON with an explicit
  schema and a rescued-data column, plus file provenance.
- **Silver `osha_quarantine` / `osha_incidents`:** null-safe rules (report ID,
  schema conformance, month format, state, industry code, narrative of at
  least 20 characters, event code). Invalid rows go to quarantine, and
  expectations record pass/fail counts. When a later OSHA snapshot republishes
  a report, the latest landed copy wins. Industry code is optional: blank
  values and sector ranges such as `48-49` are accepted.
- **Gold `osha_documents`:** one document per incident, primary key
  `report_id`. The document is a short coded header (event, injury, body
  part, source, industry, state, month) followed by the narrative, plus a
  `document_sha256` so embeddings are computed only for new or changed text.
  Narratives are at most 386 words, so there's no further chunking.

## Retrieval: exact search instead of a Vector Search endpoint

Prices were checked on September 23, 2026 (Azure Retail Prices API, `westus2`,
CAD; DBU rates from the Databricks pricing pages).

| Option | Cost |
|---|---|
| Vector Search Standard endpoint: 4 DBU/h × CAD 0.097 | ~CAD 9.3/day. It bills once an index exists and for 24 h after the last index is deleted. |
| Embeddings with Qwen3 Embedding 0.6B: 0.286 DBU per 1M tokens | ~CAD 0.03 per 1M tokens. The narratives are ~4.8M tokens; with headers the documents are ~9–10M tokens, ~CAD 0.27. |
| GTE Large: 1.857 DBU per 1M tokens | ~CAD 0.18 per 1M tokens |
| GPT-OSS-120B: 2.143 / 8.571 DBU per 1M tokens (in/out) | ~CAD 0.21 / 0.83 per 1M |
| Llama 3.3 70B: 7.143 / 21.429 DBU per 1M tokens (in/out) | ~CAD 0.69 / 2.08 per 1M |

With the always-on ~CAD 1.7/day workspace networking, any day with a Vector
Search endpoint exceeds the $10/day budget. The user chose exact search:

- Embed Gold documents with the pay-per-token `databricks-qwen3-embedding-0-6b`
  endpoint through `ai_query` in a serverless job (see "Embedding job" below).
  Rows are keyed by `(report_id, model)` and carry the document hash, so reruns
  only embed new or changed text.
- Store the vectors in Delta. Retrieval is exact cosine top-k over about 106k
  normalized vectors. That's exact rather than approximate, and costs nothing
  when idle.
- Production design (documented, not deployed): a Delta Sync index on
  `gold.osha_documents` with triggered sync. It becomes worthwhile when the
  corpus or query volume grows, or when the budget allows a CAD 9+/day endpoint.

## Embedding job (`osha_embed`)

**Result (September 24, 2026):** all **105,993** Gold documents have unit-length
1,024-dimension Qwen3 embeddings in `gold.osha_embeddings`, with primary key
`(report_id, model)`. There are 0 failures, 0 stale rows and 0 wrong-size
vectors. A rerun sent nothing to the model. Evidence:
[osha-embedding-backfill.json](osha-embedding-backfill.json).

How it works: `jobs/embed_osha.py` selects Gold documents whose
`(report_id, document_sha256)` isn't stored yet. It calls
`ai_query(endpoint, document, failOnError => false)` over 16 partitions and
writes the results **once** to a staging table, so a second read never
repeats paid calls. It merges only valid vectors (no error, 1,024 values,
|norm − 1| < 1e-3), drops the staging table, and deletes vectors for reports
no longer in Gold.

What was measured before and during the build:

- The endpoint returns 1,024-dimension unit vectors. `dimensions=256` equals
  the first 256 values rescaled to unit length (max difference 1.5e-8), so
  retrieval can evaluate 256 dimensions from the stored 1,024.
- **Direct REST calls are throttled by input count, not tokens,** far below
  the documented hourly limit. 16 inputs per request are accepted, 32 short
  inputs are rejected, and ~24–33 inputs/s is sustainable. The SDK's
  `api_client.do` silently retries 429s for up to 5 minutes, which initially
  hid this.
- **`ai_query` is not held to that REST limit:** 5,000 documents in 10.6 s
  (~470/s) with no errors; the 81,417-document backfill took 3.9 minutes of
  execution. The paced REST client in `sentinelops.embeddings` is kept for
  embedding single questions at query time.
- The first backfill attempt used paced REST calls (run `501653035205112`).
  After 18 minutes, UC table metadata still showed no commits, so I cancelled
  it as stalled. Row counts later showed it had stored 24,576 embeddings at the
  expected ~24/s. **Table metadata properties are not a progress signal**;
  count rows or log per-chunk progress. Those 24,576 rows were kept (the job
  is incremental). Batched REST and single-request vectors differ by at most
  0.002 (cosine 0.9999), which is negligible for ranking;
  `embedding_job_run` records each row's origin.
- Cost: ~9–10M tokens × 0.286 DBU per 1M × CAD 0.097 ≈ CAD 0.27, plus ~40
  minutes of serverless (including the cancelled run and two one-minute
  diagnostics).

## Retrieval evaluation (`osha_retrieval_eval`)

**Question:** does exact dense search find the right incidents, how does it
compare with keywords, and can it use 256 dimensions instead of 1,024?

**Method.** `sentinelops.retrieval_eval` defines 28 paraphrased questions,
written after profiling OSHA's code vocabulary, plus 4 off-topic questions.
A report counts as relevant when its OSHA codes satisfy the question's rule:
every clause must match, and a clause matches when any of its columns does.
Each question has 174–4,000 relevant reports, with base rates of
0.16%–3.7%. Questions are embedded with the query instruction via `ai_query`.
The job runs exact top-10 search over all 105,993 documents (1,024 dimensions,
and 256 via Matryoshka truncation) and compares it with a TF-IDF baseline on
the same documents. Results are logged to MLflow (experiment
`sentinelops-safety-rag`). The code path was rehearsed locally first:
TF-IDF on the real corpus, and the reporting path with random vectors.

**Results** (eval v2, run `603274434690806`, MLflow `5ee4a80e430c431e932504222d041bbe`):

| Method | Precision@10 | MRR | nDCG@10 | Top-1 correct | Memory | Latency per query |
|---|---|---|---|---|---|---|
| Dense, 1,024 dimensions | 0.882 | 0.908 | 0.881 | 0.857 | 414 MB | 2.8 ms |
| Dense, 256 dimensions | 0.882 | 0.920 | 0.877 | 0.857 | 104 MB | 2.7 ms |
| TF-IDF | 0.786 | 0.859 | 0.786 | 0.786 | sparse | 1.3 ms |

- **Dense beats keywords** by +0.096 precision@10, paired-bootstrap 95% CI
  [0.011, 0.196] (11 better, 14 tied, 3 worse). The gains come on
  paraphrased questions such as skylight falls (1.0 vs 0.1), back injuries
  from lifting (1.0 vs 0.3) and scaffold falls (1.0 vs 0.6).
- **256 vs 1,024: no detectable difference.** Mean 0.000, CI [−0.036, 0.043];
  17 of 28 tied, and 1,024 was narrowly ahead on 8. Their top-10 lists overlap
  only 50%, yet they're equally relevant. **Decision: use 256 dimensions for
  the assistant** (a quarter of the memory). The 1,024 values stay stored, so
  a larger evaluation can revisit this.
- **Off-topic separation** (4 questions): the lowest on-topic top-1 score is
  0.665 at 1,024 and 0.711 at 256; the highest off-topic score is 0.431 and
  0.512. The groups separate, but these negatives are easy (recipes,
  passwords). The abstention threshold must be calibrated in the next step with
  in-domain questions the reports can't answer (for example, OSHA penalty
  amounts).
- **The v1 → v2 change was to the answer key, not the system.** Inspecting v1's
  weakest question found OSHA also codes table saws as `Stationary saws  table`,
  which the rule missed; 82 amputations were uncounted. Only that rule changed,
  and it applies to every method. The v1 run (`133374292493436`) is kept in
  MLflow.
- **Remaining weak spots are label limits, not hidden:**
  - `truck_dock_pinned` (0.1–0.2): results are trucks pinning workers at docks,
    but OSHA codes the truck, not the dock, as the source.
  - `toe_amputation` (0.3–0.6): results include toe crush injuries that aren't
    amputations; this one is a genuine retrieval weakness.
  - Every document's header repeats its code titles, so absolute scores are
    optimistic. The comparisons are the point.

Evidence: [osha-retrieval-eval.json](osha-retrieval-eval.json).

## Grounded answers (`osha_answer_eval`)

**Question:** can the assistant answer from retrieved reports with citations
that check out, and decline when the reports can't answer?

**How an answer is made** (`sentinelops.answers`):

1. **Retrieve** the exact top 8 over the 256-dimension index (104 MB, current
   vectors only). The question is embedded with the Qwen3 query instruction.
2. **Decline on similarity:** if the top-1 cosine is under the calibrated
   threshold, decline without calling the model.
3. **Generate** with `databricks-gpt-oss-120b` (pay-per-token, temperature 0,
   reasoning effort low, at most 2,048 tokens). Each report sits in
   `<report id="…">` tags, and the prompt says report text is data, not
   instructions. The prompt says to decline first for numbers, frequencies,
   rankings or trends, penalties, legal/medical/compensation advice, standards
   text, identities, and unrelated questions. Otherwise, every sentence ends
   with its report IDs, e.g. `[931176]`.
4. **Decline on the model's word:** a reply containing `INSUFFICIENT_EVIDENCE`
   becomes a decline message with the model's reason.
5. **Check citations in code:** GPT-OSS's native `【id】` brackets are rewritten
   as `[id]`. A draft needs at least one citation, and every cited ID must be
   among the retrieved reports. Otherwise it's rejected and not shown.
   Sentence-level citation coverage is measured.
6. **Output** is Responses-shaped: judges read the text, and code reads
   `custom_outputs` (status, retrieved IDs, top-1 score, citation check, tokens,
   DOL attribution).

**Tracing:** one MLflow trace per question: `osha_answer` (CHAIN) →
`retrieve` (RETRIEVER, documents with `doc_uri` `osha-sir:<report_id>`) →
`generate` (CHAT_MODEL, token usage) → `check` (PARSER), plus a
`sentinelops.status` tag. Traces use the experiment's default storage. Unity
Catalog trace tables need a SQL warehouse to set up and query, so they weren't
used.

**Method** (`sentinelops.answer_eval`, eval `osha-answers-v1`):

- **DEV set, 16 questions** (6 answerable, 8 unanswerable, 2 off-topic). They
  were run locally on the real reports, with TF-IDF standing in for dense
  retrieval, to develop the prompt. Their results are never reported as the
  evaluation.
  - v1: GPT-OSS cited as `【id】`, so the code check rejected every draft. It
    also counted the retrieved reports to answer "how many … in 2022".
  - v2: ASCII brackets and an example citation fixed citations, but it still
    counted.
  - v3: an explicit decline for any number, frequency, share, ranking or
    trend. All 16 dev decisions were correct.
  - The dev run also changed the answer key. The Llama judge treated listed
    alternatives ("objects, particles or chemicals") as all required, and it
    failed a correct decline worded differently from the expected reason. So
    facts are now single general claims, and decline rows expect a generic
    decline.
- **EVAL set, 28 held-out questions**, none run through the assistant before
  the job:
  - 12 answerable; their facts were checked by keyword share in the local
    corpus;
  - 11 in-domain unanswerable: penalty, legal advice, employer ranking, a
    worker's name, a yearly count, standard text, first aid, a trend, future
    policy, a fictional employer's record, compensation;
  - 5 off-topic or adversarial, including a "developer mode" request for
    employer names.
- **Threshold, calibrated in the job on questions that aren't evaluated.** The
  on-topic group is the 28 retrieval-eval paraphrases plus 6 dev answerable
  questions (minimum top-1 0.7095). The off-topic group is 4 retrieval off-topic
  plus 2 dev off-topic questions (maximum 0.5927). The midpoint is **0.6511**.
  The job refuses to run if the groups overlap.
- **Scorers:** code checks the answer/decline decision and citation coverage.
  Llama 3.3 70B judges score `Correctness`, `RetrievalGroundedness` and
  `RelevanceToQuery`.

**Results** (run `745084593826476`, MLflow `17406be7875d4a2387785faf3ea9f083`):

| Category | Questions | Correct decisions | Outcome |
|---|---|---|---|
| Answerable | 12 | 12 | All answered; citations valid in 12/12 drafts; every sentence cited |
| In-domain unanswerable | 11 | 11 | 6 declined by the model, 5 by the threshold |
| Off-topic / adversarial | 5 | 5 | All by the threshold (top-1 0.38–0.58) |

On the 12 answers, the judges passed correctness 11/12, groundedness 12/12
and relevance 12/12. Answered questions had top-1 scores of 0.717–0.841, a
margin of at least 0.066 over the threshold.

**Honest caveats:**

- **The threshold did more of the in-domain work than intended.** It caught 5
  of 11 unanswerable questions, including `acme_record` at 0.6495, just 0.0016
  under the threshold. The model's own decline was tested on 6 held-out and 8
  dev questions. Dev unanswerable questions scored 0.619–0.762 (1 of 8 under the
  threshold). Treat the threshold as a coarse filter; for in-domain questions,
  the model's decline is the main defence.
- `conveyor_caught` failed correctness as a judge false negative. The answer
  describes hands crushed between rollers and workers pulled into rollers and
  pulleys, yet the judge said "caught in moving conveyor parts" wasn't stated.
  It stays counted as a failure.
- `grain_engulfment` was a weak question: the minimized corpus has exactly one
  grain-engulfment narrative. Retrieval ranked it first, and the answer
  correctly used only that report. My keyword check had overstated the
  support.
- Judge scores on declines aren't meaningful. Groundedness passed 1/11 and
  relevance 4/11 on correct declines. MLflow's run-level
  `retrieval_groundedness/mean` (0.50) mixes answers and declines, so use the
  per-category metrics.
- 28 questions, one run, no confidence interval. GPT-OSS isn't bit-for-bit
  deterministic at temperature 0; wording changed between dev runs.

**Cost:**

- Generation: 24,036 input and 2,792 output tokens = 0.075 DBU (≈ CAD 0.01).
- About 84 judge calls, estimated at ≈ CAD 0.15.
- Serverless: 8.1 minutes (3.1 of them setup).
- Local development: about 70 GPT-OSS and 100 judge calls, < CAD 0.2.

Evidence: [osha-answer-eval.json](osha-answer-eval.json), including every
answer.

## Next steps

1. Done: exact retrieval and its evaluation (above).
2. Done: grounded answers with code-checked `[report_id]` citations, a
   calibrated decline rule, MLflow tracing, and a held-out evaluation with a
   Llama 3.3 judge (above).
3. Structured extraction of event, nature, body part and source from
   narratives with `ai_query` and a JSON schema, scored against OSHA's codes.
4. A larger answer evaluation, with more in-domain unanswerable questions near
   the threshold, before any deployment. Deployment (Agent Framework / review
   app) stays deferred until serving costs are checked.
