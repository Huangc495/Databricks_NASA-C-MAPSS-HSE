# SentinelOps — instructions for the next session

Last updated: September 23, 2026 (medallion training continuation).

## Latest continuation — incremental checks done; training reads Gold

Read `docs/STATUS.md` (current milestone) and `docs/INGESTION.md` first.
Pipeline `77ecd502-9283-4528-83e3-7ab8666ada1e`; jobs `cmapss_ingest`
`264959928637120`, `cmapss_verify` `366011234786265`, new `cmapss_train`
`477632929595835`. All are STANDARD, manual, one concurrent run, zero retries.
Pipeline development mode must stay disabled via
`targets.dev.presets.pipelines_development: false`.

Verified on September 23, 2026:
- The quality probe **is uploaded** and permanent: Bronze +9 lines, quarantine 5,
  conflicts 1, canonical counts/features unchanged (runs `452160343690169`,
  `205718877304121`). Do not upload it again or reset checkpoints.
- A no-input rerun (`1108975989071922`) appended 0 rows; every materialized
  view was planned NO_OP.
- `cmapss_train` run `174998841420766` trained from Gold, logged Gold dataset
  inputs/digests, registered **version 3** and moved `challenger` to it
  (v2 kept, READY). v3 test RMSE 18.3415 vs v2 18.1586. The gap comes from
  Spark vs pandas rolling-mean floating-point differences (<4e-12), reproduced
  bit-for-bit offline. It is not a defect. Do not pick v2/v3 by test RMSE.
- Twelve local tests pass. Evidence: `docs/medallion-probe-update.json`,
  `docs/medallion-probe-verification.json`, `docs/medallion-rerun-update.json`,
  `docs/medallion-training.json`.

Costs: serverless wall-clock on September 23 was ~85 minutes including resource
waits. Azure still posted only CAD 0.434912152 because billing is delayed.
`system.billing` remains inaccessible (no privileges changed). Recheck posted
costs on September 24 before new compute. Next: operational ML on the Gold
contract (see STATUS "Scope still pending").

## Objective and authorization

Continue implementing the full portfolio project described in `SentinelOps.md`.
The first predictive-maintenance bootstrap is complete and verified in Azure.
Continue from these assets rather than recreating the workspace or baseline.

The user authorized Azure work under `cheng.huang.ca@outlook.com` and explicitly
selected **up to $10/day for the full demo**. Preserve this constraint. There is
no hard billing cutoff or automated budget alert; inspect posted costs before
launching further compute, account for delayed billing, and avoid leaving
Event Hubs, Vector Search, serving endpoints or SQL compute running unattended.
Normal environment/sandbox approval requirements still apply.

Do not modify the unrelated `rg-equity-silver-mlops` resources. No credentials
are stored in this handover. Use the existing Azure CLI authentication and
reauthenticate interactively only if it has expired.

## Read first

1. `SentinelOps.md` — complete intended scope; some API/product names and source
   availability need verification against current official documentation.
2. `docs/STATUS.md` — verified results, deployment history and pending work.
3. `README.md`, `docs/ARCHITECTURE.md` — commands, design and limitations.
4. `databricks.yml`, `resources/jobs.yml`, `jobs/fd001.py` and
   `src/sentinelops/` — actual implementation.

## Workspace and tools

- Project directory: `C:\Users\cheng\Local Documents\Teck\Azure Databricks Project`
- Shell: PowerShell; local Python: `.venv/Scripts/python.exe` (Python 3.12).
- Azure CLI installed globally; `uv` and Git installed.
- Databricks CLI: `.tools/databricks/databricks.exe`, version 1.17.0.
  Downloaded from the official GitHub release; archive SHA256 verified as
  `e3edc115f2aa714eac2a1591a27045cffb3c9e47b1b6da20ed2df083b42d1c98`.
- Bicep compiler installed through Azure CLI.
- Git branch `main` holds the committed project (initial commit `a75d278`,
  authored as Cheng Huang <cheng.huang.ca@outlook.com> via repo-local config).
  **No remote exists**, so nothing has been pushed.
- Data, artifacts, virtual environment, CLI binaries and compiled Bicep JSON
  are ignored by Git. They exist locally and need not be downloaded again.
- The final documentation additions may be newer than the cloud bundle sync;
  the successful run used the final application/serialization code.

Sandbox notes: Azure CLI writes logs/authentication cache outside the project,
`uv` uses an external cache, and sandboxed SciPy imports encountered Windows
Application Control errors. Tests and authenticated/network commands succeeded
with approved `require_escalated` execution. Retry genuine sandbox failures with
the normal approval mechanism; do not bypass controls or expose tokens.

## Existing Azure and Databricks resources

| Resource | Value |
|---|---|
| Subscription | Azure subscription 1 |
| Subscription ID | `b1026367-46bf-43e0-93b5-bbfcc45a2291` |
| Region | `westus2` |
| Resource groups | `rg-sentinelops-dev`, `rg-sentinelops-dev-managed` |
| Workspace name | `dbw-sentinelops-dev` |
| Workspace URL | https://adb-7405619144539463.3.azuredatabricks.net |
| Workspace ID | `7405619144539463` |
| ADLS Gen2 account | `stsent7s5fwynthfd64` |
| Containers | `landing`, `bronze`, `silver`, `gold`, `metastore` |
| Access connector | `ac-sentinelops-dev` |
| UC credential | `sentinelops_adls` |
| UC external location | `sentinelops_metastore` |
| UC catalog | `sentinelops_dev` |
| Current bootstrap schema | `sentinelops_dev` (same name as catalog) |
| Managed volume | `sentinelops_dev.sentinelops_dev.landing` |
| Bundle / target | `sentinelops` / `dev` |
| Job resource key / ID | `fd001_baseline` / `446848359557450` |
| Successful run | `99784281689509` |
| MLflow experiment ID | `2331823695746663` |
| MLflow run ID | `d4cb1f49d8444811999bbaa4dcccbfc0` |
| Registered model | `sentinelops_dev.sentinelops_dev.turbofan_rul` |
| Bootstrap model version | **2**, **READY**, no alias (was challenger) |
| Gold-trained version / alias | **3**, **READY**, **challenger** (`cmapss_train` `477632929595835`, run `174998841420766`) |

The external location points to
`abfss://metastore@stsent7s5fwynthfd64.dfs.core.windows.net/`.
Catalog managed storage uses its `/dev` subdirectory. The access connector has
Storage Blob Data Contributor scoped to this storage account. The catalog is
bound exclusively to workspace `7405619144539463`; predictive optimization is
explicitly disabled. Storage shared keys and anonymous blob access are disabled.
Public authenticated service endpoints remain enabled; Private Link/VNet
hardening is not implemented.

Azure infrastructure is defined in `infra/main.bicep`. The credential, external
location, catalog and workspace binding were created separately with the CLI;
do not assume the Bicep deployment alone recreates Unity Catalog setup.
Supporting request files are in `infra/uc-*.json`.

## Verified implementation

The job performs a checksum-verified NASA FD001 download, strict parsing,
past-only sensor feature engineering, engine-disjoint model selection, final
training/evaluation, Delta writes, MLflow tracking and UC challenger registration.

Three managed Delta tables exist in `sentinelops_dev.sentinelops_dev`:

- `silver_fd001_train`
- `gold_fd001_features`
- `gold_fd001_predictions`

These are bootstrap batch overwrites. The new medallion tables are separate from
these original tables; see the latest continuation above. The prediction table contains benchmark test outputs;
it is not a production batch-scoring workflow for new fleet observations.

Verified cloud and local metrics match exactly:

- FD001: 20,631 training rows, 13,096 test rows, 100 test-engine endpoints.
- Test RMSE: **18.1585984491 cycles**, constant baseline **43.0670355425**.
- MAE: **13.2353650435**; NASA asymmetric score: **629.0335010946**.
- Five tests passed, including causal feature boundaries and MLflow/skops
  save/reload prediction equivalence.
- Strict bundle validation and cloud deployment passed.
- Successful cloud run completed in about 8 minutes 9 seconds, including startup.

Artifacts: `artifacts/fd001/{metrics.json,predictions.csv,model.pkl}`;
tracked report/chart: `docs/fd001-benchmark.json`, `docs/fd001-benchmark.png`.

Evaluation contract: training labels capped at 125 cycles; official test labels
remain uncapped. Split by engine, never random rows. Tune only on held-out
training engines, not repeatedly against official test labels. Features include
current readings, trailing means and past differences; unit ID and RUL are not
predictors. The registered model expects engineered features, not one raw sensor
row without its history. Preserve this contract when implementing serving.

## Runtime fixes already resolved — do not undo

1. Databricks serverless Python tasks did not define `__file__`. The bundle now
   passes `--source-root ${workspace.file_path}/src` explicitly to the job.
2. MLflow 3.16.1 defaults to skops serialization. Our fitted histogram gradient
   boosting model requires a specific trusted sklearn class. The policy in
   `src/sentinelops/registry.py` permits only
   `sklearn.ensemble._hist_gradient_boosting.predictor.TreePredictor` for models
   created by our own training code. Do not infer trust from arbitrary files or
   broaden the list automatically. `tests/test_serialization.py` verifies it.
3. Serverless environment version **4** gives Python 3.12. The earlier version 1
   used Python 3.10 and produced slightly different results. Cloud dependencies
   are pinned: numpy 2.5.3, pandas 2.3.3, scikit-learn 1.9.1, MLflow 3.16.1,
   skops 0.15.0. Keep cloud/local versions consistent.

`requirements-lock.txt` records the original core local benchmark dependencies.
It is not a complete lock of the subsequently added MLflow stack; those direct
versions are pinned in the project extra and job definition.

## Resume commands

Run from the project directory:

```powershell
. ./scripts/Use-SentinelOps.ps1
az account show --query '{name:name,id:id,user:user.name}' -o json
./scripts/Inspect-Run.ps1 -RunId 99784281689509
.tools/databricks/databricks.exe model-versions get-by-alias sentinelops_dev.sentinelops_dev.turbofan_rul challenger -o json
.venv/Scripts/python.exe -m pytest -q
.tools/databricks/databricks.exe bundle validate --strict -t dev
```

Only rerun training or redeploy when a change or unresolved check warrants it:

```powershell
.tools/databricks/databricks.exe bundle deploy -t dev
.tools/databricks/databricks.exe bundle run -t dev fd001_baseline --no-wait -o json
```

Inspect posted costs without starting a SQL warehouse:

```powershell
az rest --method post --url 'https://management.azure.com/subscriptions/b1026367-46bf-43e0-93b5-bbfcc45a2291/providers/Microsoft.CostManagement/query?api-version=2023-03-01' --body '@infra/cost-query.json' --query properties -o json
```

The last cost query returned no posted rows; this was **not evidence of zero
charges**. `docs/cost-review.sql` is an optional, not-yet-executed list-price
Databricks billing query; Azure infrastructure costs must also be included.
The last compute inventory found no classic clusters and a stopped starter SQL
warehouse with a 10-minute auto-stop. Verify current state rather than assuming
it is unchanged. The configured job uses STANDARD mode, max one concurrent run,
zero application retries, 900-second timeout, and no recurring schedule.

## Next work, in order

1. **Production-shaped ingestion and medallion path:** done for FD001 (probe,
   no-input rerun, Gold-fed training all verified). Use
   `dataset/subset + unit + cycle` keys when extending beyond FD001. Keep train
   and test trajectories separate; test RUL must come from official endpoint
   labels, not the maximum observed test cycle.
2. **Operational ML:** implement scoring of new observations, serving with a
   defined feature/history contract, inference logging, monitoring with delayed
   ground truth, and validation-based champion/challenger promotion/retraining.
   Compute scoring features with the same Spark logic as Gold, not pandas: the
   model is sensitive to their ~1e-12 differences. There is currently no
   champion alias, serving endpoint or monitoring setup.
3. **Safety RAG:** verify/download OSHA sources, record provenance and license,
   handle unnecessary personal/address data, build chunks and retrieval, add
   grounded answers with citations/abstention, tracing/evaluations and structured
   extraction. Verify affordable regional Vector Search/model availability first.
4. **Remaining platform:** API ingestion and Event Hubs streaming, AI/BI dashboard
   and Genie, environment isolation and service-principal/OIDC staging/prod
   delivery, appropriate networking/governance, demo script and portfolio polish.

Current CI is a local GitHub Actions workflow definition running Python tests;
no GitHub repository, remote, OIDC federation or CD deployment has been created.
Do not claim those capabilities as deployed. Start each next milestone by checking
the relevant current official API documentation. Update `docs/STATUS.md` with
actual verification evidence, resource IDs, costs when available, and any gaps.
