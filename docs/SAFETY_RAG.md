# Safety GenAI assistant: sources, privacy and retrieval design

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

## Next steps

1. Retrieval module and a small labelled retrieval evaluation set built from
   OSHA's own classification codes (for example, "amputation incidents
   involving presses"), comparing 1,024 and 256 dimensions.
2. Grounded answers from GPT-OSS-120B with inline `[report_id]` citations, an
   abstention rule, and MLflow tracing.
3. Evaluation: correctness, groundedness and relevance using an LLM judge from
   a different model family (Llama 3.3 70B), plus citation validity checks.
4. Structured extraction of event, nature, body part and source from
   narratives, scored against OSHA's codes.
