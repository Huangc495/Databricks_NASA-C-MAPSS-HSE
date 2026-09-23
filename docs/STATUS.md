# Build status

Last verified: September 23, 2026.

## Current milestone: incremental checks passed; training consumes Gold

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

## Previous milestone: medallion ingestion and feature parity verified

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

1. Operational ML on the Gold contract: batch scoring of new observations from
   Spark-computed features (not pandas, given the numerical sensitivity above),
   serving with a defined history contract, inference logging, monitoring with
   delayed labels, and promotion gated on fresh validation data, never on the
   fixed official test set. No champion alias exists yet. Consider orchestrating
   ingest → verify → train as one job to avoid concurrent Gold reads.
   Additional external locations are deferred until needed.
2. Extend beyond FD001 (FD002–FD004 need operating-condition handling); the
   `(dataset, subset, split, unit, cycle)` keys and `--subset` parameter support it.
3. Build OSHA safety RAG with retrieval, citations, privacy handling and evaluation.
4. Add Event Hubs/API ingestion, dashboard/Genie, staging/production and OIDC CI/CD.

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

The local benchmark demonstrates predictive performance on simulated engines.
It is not evidence of reduced real-world downtime or an operational safety system.
Azure budgets provide alerts, not a hard spending cutoff. This project currently
has no recurring compute schedule and does not provision persistent AI endpoints.
