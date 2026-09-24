# Build status

Last verified: September 24, 2026.

## Task status

This is the single tracker for the full scope in [SentinelOps.md](../SentinelOps.md).
**Done** means verified in Azure (or locally, where noted) with evidence in
this file. **Next** marks the agreed order. **Deferred** means held back for
cost or prerequisites, with the reason given.

### Platform and governance

| Task | Status | Evidence or next action |
|---|---|---|
| Azure foundation: ADLS Gen2, workspace, access connector, UC catalog | Done | `infra/main.bicep`; "Azure" section below |
| Bundle deployment (dev target, strict validation, manual jobs) | Done | `databricks.yml`, `resources/*.yml`; 11 jobs and pipelines deployed |
| Cost visibility: meter-level Azure cost query | Done | Found an always-on NAT gateway/IP, ~CAD 1.7/day ("Cost and runtime controls") |
| Azure budget alert | Not started | Cheap safeguard for the $10/day limit; needs your approval to create |
| Databricks billing tables (`system.billing`) access | Not started | Needs an account/metastore admin grant |
| dev/staging/prod catalogs, service principals, `run_as` | Not started | After the demo features are complete |
| Secrets in Key Vault or a secret scope | Not started | Needed once API keys or Event Hubs credentials exist |
| Private Link / VNet hardening | Deferred | Cost and complexity; public endpoints use authenticated access only |

### Data engineering

| Task | Status | Evidence or next action |
|---|---|---|
| C-MAPSS FD001 Auto Loader + Lakeflow medallion with quarantine/conflicts | Done | Pipeline `77ecd502…`; parity verified |
| Incremental ingestion probe and no-input rerun | Done | `medallion-probe-*.json`, `medallion-rerun-update.json` |
| FD002–FD004 (multiple operating conditions) | Not started | Keys and `--subset` already support it; needs condition-aware features |
| REST API ingestion (e.g. weather/energy JSON) | Not started | |
| Event Hubs (Kafka endpoint) streaming | Not started | Bills while the namespace exists: demo only, then delete |

### Predictive-maintenance ML

| Task | Status | Evidence or next action |
|---|---|---|
| Training from Gold with dataset lineage | Done | Version 3; `medallion-training.json` |
| Validation-gated promotion (never reads test labels) | Done | v3 is `@champion`; `promotion-decision.json` |
| Fleet batch scoring into an idempotent inference log | Done | `gold.cmapss_predictions`; `fleet-scoring-*.json` |
| Delayed-label performance and age-matched drift monitoring | Done | `gold.cmapss_model_performance`, `gold.cmapss_feature_drift` |
| Alerts on drift and performance tables | Not started | Needs thresholds, and a SQL warehouse only while an alert evaluates |
| Orchestrated retraining (ingest → verify → train → promote → score) | Not started | Also prevents concurrent Gold reads |
| Real-time serving demo with Gold-format features | Not started | Bounded demo; delete the endpoint afterwards |
| Hyperparameter tuning, `mlflow.evaluate`, sequence baseline | Not started | |
| Lakehouse Monitoring inference profile | Not started (optional) | Job-computed metrics already cover the demo |

### Safety GenAI assistant (OSHA)

| Task | Status | Evidence or next action |
|---|---|---|
| Source, license and provenance (checksum-pinned) | Done | [SAFETY_RAG.md](SAFETY_RAG.md) |
| Privacy minimization before upload | Done | 282 narratives masked; identifying columns dropped |
| OSHA medallion pipeline to Gold `osha_documents` | Done | 105,993 documents, 3 quarantined |
| Retrieval cost decision (no Vector Search endpoint) | Done | Chosen by you; endpoint would be ~CAD 9.3/day |
| Document embeddings (`osha_embed`) | Done | 105,993 vectors; `osha-embedding-backfill.json` |
| Exact retrieval + code-based retrieval evaluation (1,024 vs 256 dimensions) | Done | Dense P@10 0.882 vs TF-IDF 0.786; 256 dims = 1,024 quality at 1/4 memory; `osha-retrieval-eval.json` |
| Grounded answers with `[report_id]` citations, abstention, MLflow tracing | **Next** | GPT-OSS-120B (pay-per-token), 256-dim retrieval; calibrate abstention on in-domain unanswerable questions |
| LLM-judge evaluation (correctness, groundedness, relevance) | Not started | Judge from a different model family (Llama 3.3 70B) |
| Structured extraction scored against OSHA codes | Not started | |
| Agent deployment / review app | Deferred | Check serving cost first; scale-to-zero only |

### Analytics and delivery

| Task | Status | Evidence or next action |
|---|---|---|
| Unit tests (36) and local CI workflow file | Done (local) | `.github/workflows/ci.yml` has never run: no remote |
| Git history | Done (local) | Branch `main`; no remote |
| GitHub repository, CI runs, OIDC deployment to staging/prod | Not started | Needs your choice of repository and visibility |
| AI/BI dashboard and Genie space | Not started | Viewing uses SQL warehouse time |
| Demo script and portfolio write-up | Not started | Last |

Recommended order: grounded answers and evaluation →
structured extraction → orchestrated retraining and alerts → dashboard/Genie →
serving demo → API/streaming sources → environments and CI/CD.

## Current milestone: Safety GenAI retrieval evaluation (OSHA)

Exact dense search is validated against OSHA-code relevance and beats a
keyword baseline. 256 dimensions are chosen for the assistant. Details:
[SAFETY_RAG.md](SAFETY_RAG.md#retrieval-evaluation-osha_retrieval_eval).

- **Job `osha_retrieval_eval`** (`383639217735446`), eval v2, run
  `603274434690806` **SUCCESS**, MLflow run `5ee4a80e430c431e932504222d041bbe`
  (experiment `sentinelops-safety-rag`). The questions are 28 paraphrases whose
  relevance comes from OSHA code rules, plus 4 off-topic ones, searched
  exactly over all 105,993 documents.
- **Precision@10:** dense-1,024 **0.882**, dense-256 **0.882**, TF-IDF 0.786.
  Dense minus TF-IDF is +0.096, 95% CI [0.011, 0.196]. 256 minus 1,024 is
  0.000, CI [−0.036, 0.043]. The 256-dimension index uses 104 MB instead of
  414 MB, at ~2.7 ms per query.
- **Off-topic separation** holds at both sizes (lowest on-topic top-1 0.665
  and 0.711; highest off-topic 0.431 and 0.512). The negatives are easy, so
  abstention still needs calibration.
- **Honest caveats:** v1 → v2 fixed one answer-key vocabulary bug (OSHA's
  reversed title `Stationary saws  table`); v1 run `133374292493436` is kept.
  Two weak questions reflect code-label limits (`truck_dock_pinned`) or a
  genuine retrieval weakness (`toe_amputation`). Headers repeat the code
  titles, so absolute scores are optimistic.
- The code path was rehearsed locally before cloud runs: TF-IDF on the real
  corpus (cloud results matched it exactly), and the reporting path with
  random vectors. 36 local tests pass (7 new).
- Cost: two ~6-minute serverless runs; question embeddings were negligible.

## Earlier milestone: Safety GenAI embeddings (OSHA)

All 105,993 Gold OSHA documents now have embeddings for exact retrieval. No
answers are generated yet. Details: [SAFETY_RAG.md](SAFETY_RAG.md#embedding-job-osha_embed).

- **Job `osha_embed`** (`379147303783128`) uses `ai_query` against the
  pay-per-token `databricks-qwen3-embedding-0-6b` endpoint. Results are staged
  once, and only validated vectors are merged. Run `942266880002897`
  **SUCCESS**: 81,417 embedded, 0 failed, 3.9 min of execution. `gold.osha_embeddings`
  stores **105,993** vectors, equal to the Gold document count, with 0 stale
  and 0 wrong-size. Rerun `206063629553445` **SUCCESS**: 0 pending, no model
  calls. Evidence: [osha-embedding-backfill.json](osha-embedding-backfill.json).
- **Measured:** direct REST calls to the endpoint are throttled by input count
  (16 per request accepted, 32 rejected; ~24–33 inputs/s), far below the
  published hourly limit. `ai_query` ran at ~470 docs/s without errors.
  Stored 1,024-dimension vectors can be truncated to 256 exactly.
- **Mistake, and its cost:** the first attempt used paced REST calls (run
  `501653035205112`). I cancelled it after 25 minutes because UC table
  metadata showed no commits, but it was in fact progressing (24,576 rows
  stored, kept). That cost ~25 minutes of compute. Lesson: count rows or log
  progress instead. Two one-minute diagnostic runs found the real behavior and
  led to `ai_query`.
- Cost (September 24): ~40 min of serverless and ~9–10M embedding tokens
  (~CAD 0.27), on top of the fixed ~CAD 1.7/day. Nothing is running afterwards.
- Tests: 29 pass (five new, for batching, pacing, throttling, isolation and
  validation).

## Earlier milestone: Safety GenAI data foundation (OSHA)

Source, license, privacy and cost checks are done. Minimized OSHA reports are
in Bronze/Silver/Gold. No embeddings or model calls yet. Design:
[SAFETY_RAG.md](SAFETY_RAG.md).

- **Source:** with the user's approval, downloaded OSHA's
  `January2015toNovember2025.zip` (16,224,511 bytes, matching the server's
  `Content-Length`). SHA-256 `a3f7f434…46bb0` is pinned. It holds one UTF-8 CSV
  with 105,996 reports (2015-01 to 2025-11). The data is a federal
  public-domain work; attribution to DOL is recorded in the manifest. `ID` is
  not unique (5 IDs cover two incidents each); `UPA` is unique and is used as
  `report_id`.
- **Privacy:** employer, address, city, ZIP, coordinates and inspection numbers
  are dropped locally before upload, and event dates are coarsened to month. In
  narratives, the report's own employer and address, numbered streets and
  state+ZIP are masked: 282 narratives, 300 replacements. Scans found no
  emails, phones, SSNs or personal names. The residual risks (dates, cities,
  other companies) are documented.
- **Cost decision (user-approved):** exact retrieval over Delta-stored
  embeddings, no Vector Search endpoint. A Standard endpoint costs ~CAD 9.3/day
  and keeps billing for 24 h after the last index is deleted, which would
  exceed the $10/day budget. Qwen3 embeddings for the full corpus are estimated
  at ~CAD 0.15 in tokens.
- **Pipeline `osha_safety`** (`7d53a0fb-d724-4628-a854-23dc1d0e283a`), job
  `osha_ingest` (`1070808576153729`):
  - Run `1007139224127625` **SUCCESS**. Bronze 105,996 rows, schema-conformant
    (nothing rescued). 6 rows quarantined: 3 near-empty narratives, 2 blank
    industry codes, and 1 sector range `48-49`.
  - The industry-code rule was then relaxed, because industry is optional
    metadata. Run `197582638913248` **SUCCESS**: Bronze appended 0 rows (the
    checkpoint held), quarantine is **3** (only narratives under 20 characters),
    and Silver `osha_incidents` and Gold `osha_documents` (primary key
    `report_id`) each have **105,993** rows.
  - Evidence: [osha-first-update.json](osha-first-update.json),
    [osha-rule-update.json](osha-rule-update.json).
- **Tests:** 24 local tests pass (four new OSHA tests: masking, minimization,
  checksum, immutability). Real data exposed two issues before upload, both
  fixed: blank severity counts, and ZIP codes and numbered streets in narratives.

## Previous milestone: batch operational ML (promote, score, monitor)

A champion now scores the simulated fleet into an idempotent inference log.
Delayed labels are merged, and performance and drift snapshots are recorded.
See [OPERATIONS.md](OPERATIONS.md) for the design. No Azure resources were created.

- **Promotion gate:** job `cmapss_promote` (`714826690927619`), run
  `268300947742291` **SUCCESS**. v3 passed every gate: Gold lineage, feature
  signature, and label digest unchanged. Validation RMSE **14.9309** vs a
  constant baseline of **41.7208** on the same 20 held-out engines (ratio 0.358;
  the limit is 0.5). No official test labels were read. **`@champion` → v3**;
  `@challenger` removed; v3 tagged `promotion_decision=promoted`. Confirmed
  independently via the UC API. Evidence: [promotion-decision.json](promotion-decision.json).
- **Fleet scoring + monitoring:** job `cmapss_score` (`362250970463849`), run
  `232258023004472` **SUCCESS** (9.3 min, both tasks). Scored **13,096** fleet
  (test-split) observations into `gold.cmapss_predictions`, then merged 13,096
  delayed labels. Endpoint-segment RMSE is **18.341479192292436**, bit-identical
  to v3's training-run test RMSE. That confirms scoring uses exactly the
  evaluated model and features. Other segments: true RUL ≤ 125, RMSE 19.17
  (bias +5.1); true RUL > 125, bias −60, expected from the 125-cycle label cap.
  Drift: raw PSI > 0.2 for 26 of 43 features, **0 of 43** after age matching
  (max 0.102). Raw PSI mainly reflects younger fleet engines, not sensor drift.
  Evidence: [fleet-scoring-first-run.json](fleet-scoring-first-run.json).
- **Idempotent rerun:** run `94693603475272` **SUCCESS** (7.3 min). 0 pending
  rows, no scoring merge, 0 labels updated, the log still has 13,096 rows for
  v3, and performance and drift values are identical to the first run. Each run
  appends one snapshot to the performance and drift tables by design.
  Evidence: [fleet-scoring-rerun.json](fleet-scoring-rerun.json).
- All values match a local rehearsal on real FD001 data with Spark-emulated
  features, run before any cloud compute.
- Twenty local tests pass (eight new: gate, baseline, label digest, performance
  segments, PSI, age matching). The age-matching test caught an open-ended bin
  bug before deployment. Strict validation passed; deploy created only
  `cmapss_promote` and `cmapss_score`.
- Not done: no real-time serving endpoint or inference tables; no schedules
  or alerts; no Lakehouse Monitoring monitor (metrics are computed by the job).

## Earlier milestone: incremental checks passed; training consumes Gold

All outstanding medallion checks have run in the cloud, and training now reads
the Gold tables. No Azure resources were created; one bundle job was added.

- **Quality probe (incremental ingestion):** uploaded the nine-line
  `train_FD001_quality.txt`. Ingestion run `452160343690169` **SUCCESS**
  (pipeline update `def8a926-30c5-46a4-88f7-d4e3acb64c2c`). Bronze appended
  exactly **9** lines; consumed files were not re-read. `valid_trajectory` passed
  33,731 and dropped 5; quarantine **5**, conflicts **1** (unit 999, two
  payloads); equal-payload repeats collapsed; Silver observations and all Gold
  counts unchanged. Gold features and labels refreshed incrementally
  (WINDOW_FUNCTION), quarantine APPEND_ONLY; Silver observations/conflicts fully
  recomputed because expectations prevent incrementalization.
  Evidence: [medallion-probe-update.json](medallion-probe-update.json).
- **Probe verification:** run `205718877304121` **SUCCESS**: Bronze 33,736
  lines; every canonical count and all 33,727 feature values unchanged (rtol
  1e-10); quarantine rows all from the probe file; composite key confirmed.
  Evidence: [medallion-probe-verification.json](medallion-probe-verification.json).
- **No-input rerun:** ingestion run `1108975989071922` **SUCCESS** (update
  `fa19089a-4199-412b-b539-be38fd20a16e`). Both Bronze streams appended 0 rows,
  and the planner chose **NO_OP for every materialized view**, so nothing was
  rewritten. Verified from the event log rather than a third verification run,
  to save compute; the training run below then re-read all Gold tables.
  Evidence: [medallion-rerun-update.json](medallion-rerun-update.json).
- **Training from Gold:** new job `cmapss_train` (`477632929595835`), run
  `174998841420766` **SUCCESS** (6.5 min). MLflow run
  `02b4dd86f955493ab75c6a53ef685e3f` in experiment `2331823695746663` logged
  the three Gold tables as dataset inputs with content digests, upstream quality
  counts (5 quarantined, 1 conflict), metrics and predictions. Registered
  **version 3** (READY, tagged `training_source=gold_medallion`); `challenger`
  now points to v3. Version 2 remains READY without an alias.
  Evidence: [medallion-training.json](medallion-training.json).
- **Gold v3 vs bootstrap v2:** 20,631 rows, 100 engines, 7 leaves, identical
  labels (bitwise-equal digest) and constant baseline RMSE 43.0670. Test RMSE
  **18.3415** vs 18.1586, MAE **13.1701** vs 13.2354, NASA **650.81** vs 629.03,
  validation RMSE 14.9309 vs 14.8517. Root cause: Spark `avg` over the 10-row
  window sums sequentially while pandas rolling mean uses a different algorithm;
  the means differ by at most 3.6e-12 in 36% of cells. That shifts gradient
  boosting's bin thresholds. Locally emulating Spark's
  sequential-sum mean reproduces the cloud metrics **bit-for-bit**. With exact
  pandas features, the Gold code path reproduces v2 bit-for-bit (local check).
  So the ±0.2 RMSE gap is numerical sensitivity, not a data or logic error.
  Choosing between v2 and v3 by test RMSE would be tuning on the test set.
- Twelve local tests pass (five new Gold-contract tests). Strict bundle
  validation passed; deploy created only `cmapss_train`, other resources unchanged.
- STANDARD mode spent ~7 minutes waiting for resources before the probe update;
  ingestion runs took 11–12 minutes of the 15-minute limit. Margin is thin.
- Final inventory: pipeline **IDLE**, no active runs, no classic clusters,
  starter warehouse **STOPPED**, no custom serving endpoints.

## Earlier milestone: medallion ingestion and feature parity verified

- Added `pipelines/medallion.py`: raw TEXT/JSON Auto Loader, quality expectations,
  malformed-row quarantine, conflicting-key exclusion, numeric deduplication,
  causal features, training labels, and official test endpoint joins.
- Reused the existing UC volume and ADLS managed storage. Added catalog schemas
  `bronze`, `silver`, `gold`; no new Azure infrastructure or external grants.
- Pipeline ID: `77ecd502-9283-4528-83e3-7ab8666ada1e`.
- Ingestion job ID: `264959928637120`; first run `103102387857859`.
- That combined run ended **TIMEDOUT**: ingestion succeeded, but verification
  was cancelled during its separate standard-mode startup, before executing any
  assertions. Do not treat the parent run as a full verification success.
- Verification is now a separate manual job `366011234786265` (`cmapss_verify`),
  with a 600-second timeout. Run `448846224282372` **SUCCESS**.
  [Verification run](https://adb-7405619144539463.3.azuredatabricks.net/jobs/366011234786265/runs/448846224282372).
- Pipeline update `2daec6e4-74bd-4a8b-b6f1-a37e40a9f5f7` **COMPLETED**.
  Event metrics confirm 33,727 canonical observations/features, 20,631 training
  labels, 100 test endpoints, zero quarantine/conflicts, and zero failed
  expectations on the original data. UC API confirms the five-column Gold
  primary key. Independent assertions passed for every feature value across
  train/test, all table counts, both label contracts, and key uniqueness/metadata.
- Strict bundle validation and deployment passed. Existing baseline resource was
  unchanged. All **seven** local tests passed after the ingestion additions.
- Deployed pipeline is triggered, serverless, development **false**. Bundle dev
  presets otherwise override the resource setting, so the preset is explicitly
  disabled. Job performance is verified STANDARD, max concurrency one, no retries,
  900-second total timeout, no schedule.
- First-update event evidence: `docs/medallion-first-update.json`.
- Independent assertion evidence: `docs/medallion-verification.json`.
- Incremental duplicate/quarantine/conflict probe is prepared by
  `scripts/prepare_quality_probe.py`. (Superseded: the probe and a no-input
  rerun have since passed; see the current milestone above.)
- Databricks billing snapshot query was denied: current user lacks USE SCHEMA on
  `system.billing`. No privileges were changed. With Azure posting delayed costs,
  further discretionary compute was deferred to preserve the $10/day limit.
  Recheck posted costs and obtain a current usage view before more cloud tests.
- Initial cost review: Azure posted **CAD 0.434912152** for September 23, 2026,
  across both SentinelOps resource groups. This is delayed posted usage, not a
  final daily total. No classic clusters; starter SQL warehouse STOPPED.
- See [INGESTION.md](INGESTION.md) for the data contract and runbook.
- Final inventory: pipeline **IDLE**, verification run **TERMINATED/SUCCESS**,
  no classic clusters, starter SQL warehouse **STOPPED** with zero clusters.
  Final Azure posted cost query remained CAD 0.434912152.

## Bootstrap verification retained

The first cloud path has succeeded: verified FD001 download → causal features
→ model selection/training → Delta tables → MLflow tracking → Unity Catalog
model registration. This is the initial predictive-maintenance milestone, not
completion of the full SentinelOps platform.

- [Successful cloud run](https://adb-7405619144539463.3.azuredatabricks.net/jobs/446848359557450/runs/99784281689509)
  completed in approximately 8 minutes 9 seconds including startup.
- Registered model: `sentinelops_dev.sentinelops_dev.turbofan_rul`, version **2**,
  status **READY**, alias **challenger** (independently verified through the API).
- Final cloud metrics exactly match local: RMSE **18.1586**, MAE **13.2354**,
  NASA score **629.0335**. Constant baseline RMSE: **43.0670**.
- No champion promotion or serving endpoint has been created.

## Verified locally

- NASA FD001 archive downloaded and MD5 verified against the Zenodo record.
- Training: 20,631 rows / 100 engines. Test: 13,096 rows / 100 endpoints.
- Engine-disjoint 80/20 selection; random seed 42; 7-leaf gradient boosting selected.
- Official endpoint RMSE: **18.1586 cycles**; constant baseline: **43.0670**.
- MAE: **13.2354 cycles**; NASA asymmetric score: **629.0335**.
- Five tests passed (causal features, engine boundaries, labels/metric,
  whitespace/schema/duplicate checks, MLflow/skops save-and-reload equivalence).
- Azure Bicep compilation and Azure what-if validation succeeded.

## Azure

- Account: cheng.huang.ca@outlook.com
- Subscription: Azure subscription 1
- Subscription ID: b1026367-46bf-43e0-93b5-bbfcc45a2291
- Resource group: rg-sentinelops-dev
- Region: westus2
- User-approved operating budget: up to $10/day for the full demo.
- Foundation deployment **Succeeded**: workspace, ADLS, five containers,
  access connector and storage-scoped RBAC.
- Workspace: https://adb-7405619144539463.3.azuredatabricks.net
- Storage: stsent7s5fwynthfd64
- UC credential: sentinelops_adls; external location: sentinelops_metastore.
- Catalog: sentinelops_dev, managed storage on the project's ADLS account.
- Databricks CLI 1.17.0 downloaded from the official release and SHA256 verified.
- Strict bundle validation passed; training job deployed successfully.
- Job ID: 446848359557450.
- First run 24543372245565 exposed a serverless runner difference: `__file__`
  is undefined. Fixed by passing the deployed source directory explicitly.
- Run 1034452564613764 trained successfully and created three managed Delta
  tables in sentinelops_dev.sentinelops_dev. Cloud RMSE was 18.3627 cycles
  with the older Python 3.10 environment. MLflow model serialization failed
  because skops requires trust for sklearn's TreePredictor type.
- The serialization policy now trusts only that known type for the model fitted
  in this job; the local save/reload regression test passed.
- Cloud environment updated to version 4 (Python 3.12); numpy, pandas,
  scikit-learn, MLflow and skops versions pinned.
- Latest verification run: 99784281689509 — **SUCCESS**.
- MLflow run ID: d4cb1f49d8444811999bbaa4dcccbfc0.
- Existing Delta tables: silver_fd001_train, gold_fd001_features,
  gold_fd001_predictions. MLflow experiment ID: 2331823695746663.
- Catalog is bound exclusively to this workspace. Predictive optimization is
  disabled to avoid background maintenance compute.
- Job uses standard performance mode, max one concurrent run, no application
  retries, a 15-minute timeout and no recurring schedule.

## Scope still pending

See **Task status** at the top of this file. It is the single tracker; this
section is kept only so older links still resolve.

## Cost and runtime controls

- Approved operating budget: up to $10/day; no hard daily billing cutoff exists.
- Last compute inventory found no classic clusters and a stopped starter SQL
  warehouse with a 10-minute auto-stop setting.
- No recurring job schedule or persistent serving/Vector Search endpoints.
- Azure Cost Management now reports CAD 0.434912152 for September 23 across the
  two project resource groups. Billing is delayed; this does not prove the final
  daily total is within budget. Recheck using `infra/cost-query.json`.
  `system.billing` is inaccessible to this user. No automated budget alert or
  hard cutoff has been configured.
- Serverless wall-clock on September 23 (UTC), including resource waits:
  ~49 minutes before this milestone, plus ~36 minutes for it (ingest 12.2 +
  verify 6.3 + ingest 11.2 + train 6.5). Azure still posted CAD 0.434912152
  afterwards, which reflects billing delay rather than actual usage. The
  project has never had a usage view that confirms the daily total; an
  account/metastore admin granting read access to `system.billing` would fix that.
- By 17:15 UTC, cost posted for September 23 had reached **CAD 1.02**; a query
  grouped by meter covered usage up to about 08:00 UTC:
  - serverless compute: 0.687 DBU = CAD 0.43, about **CAD 0.62/DBU**, roughly
    1.5 DBU per hour of job wall-clock;
  - **a NAT gateway and a static public IP in the managed resource group bill
    24/7**, about CAD 0.07/hour, or **~CAD 1.7/day even with no compute**. They
    come from the workspace's secure-cluster-connectivity networking, which only
    classic compute uses; serverless does not use them. Keep this fixed cost in
    the daily budget;
  - storage and bandwidth are negligible.
- The operational-ML milestone added ~25 minutes of serverless wall-clock
  (promote 6.4, score+monitor 9.3, idempotency rerun 7.3). Projected September 23
  total: roughly CAD 3–4, under the $10/day limit.

The local benchmark demonstrates predictive performance on simulated engines.
It is not evidence of reduced real-world downtime or an operational safety system.
Azure budgets provide alerts, not a hard spending cutoff. This project currently
has no recurring compute schedule and does not provision persistent AI endpoints.
