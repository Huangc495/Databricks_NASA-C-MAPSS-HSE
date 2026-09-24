# SentinelOps — handover for the next session

Last updated: September 24, 2026, 19:50 UTC. Git `main` holds every milestone,
one commit each (latest: `7a863dd`, masking v2). There is no remote; see
section 6.

**Where things stand.** Both halves of the portfolio project run in Azure
Databricks, and every job is manual, bounded and verified. 28 of the 40
tracked tasks are done. The **Task status** table at the top of
[docs/STATUS.md](docs/STATUS.md) is the authoritative tracker; update it as
work lands.

- **Predictive maintenance** (NASA C-MAPSS FD001):
  - Auto Loader and Lakeflow ingestion into Bronze/Silver/Gold.
  - Training from Gold, with a validation-gated `@champion` (v3).
  - Idempotent fleet batch scoring, delayed-label and age-matched drift
    monitoring, and threshold alerts.
  - One orchestrated job, `cmapss_retrain`.
  - A bounded **real-time serving demo** (deleted afterwards): all 13,096
    fleet rows came back bit-identical to the batch log.
- **Safety GenAI assistant** (OSHA Severe Injury Reports):
  - Privacy-minimized landing, now **masking v2**, which also masks shortened
    employer names.
  - Qwen3 embeddings with exact retrieval.
  - Grounded GPT-OSS-120B answers with code-checked citations and a
    calibrated decline rule, traced in MLflow.
  - Structured extraction scored against harmonized OSHA codes.
  - **Evaluations:**
    - v1: 28 questions, 28/28 correct decisions;
    - v2: 60 questions, 58/60. It found an employer-name leak, which masking
      v2 fixed;
    - identity check: no employer names in 76 answers, and 13 of 16 identity
      requests declined.
- **Self-service analytics:** two AI/BI dashboards and a Genie space, all
  bundle resources, fed by the `analytics_refresh` job.
- **Cost guardrails:**
  - Azure budget `sentinelops-dev-monthly` (CAD 150/month, email alerts);
  - the starter SQL warehouse is 2X-Small with a 5-minute auto-stop.

**Next task:** REST API ingestion (section 3, task A). It needs the user's
choice of source and approval of the download.

## 1. Start here

1. Read, in this order:
   - this file;
   - `docs/STATUS.md`: task table, at-a-glance, current milestone;
   - `docs/SAFETY_RAG.md`: OSHA design, privacy, retrieval, answers,
     masking v2, extraction;
   - `docs/OPERATIONS.md`: promotion, scoring, monitoring, retraining,
     alerts, real-time serving;
   - `docs/INGESTION.md` (C-MAPSS contracts) and `docs/ANALYTICS.md`
     (dashboards, Genie, warehouse cost);
   - `SentinelOps.md` for the full intended scope and the demo script.
2. Verify the environment and that nothing is running (all free):

```powershell
. ./scripts/Use-SentinelOps.ps1
az account show --query '{name:name,id:id,user:user.name}' -o json
.venv/Scripts/python.exe -m pytest -q                      # expect 92 passed
.tools/databricks/databricks.exe bundle validate --strict -t dev
.tools/databricks/databricks.exe bundle plan -t dev        # expect only the 3 known no-op job "updates" (section 4)
.tools/databricks/databricks.exe jobs list-runs --active-only -o json
.tools/databricks/databricks.exe clusters list -o json
.tools/databricks/databricks.exe warehouses list -o json   # starter warehouse: STOPPED, 2X-Small, auto-stop 5
.tools/databricks/databricks.exe serving-endpoints list -o json   # only databricks-* endpoints
.tools/databricks/databricks.exe vector-search-endpoints list-endpoints -o json   # expect none
.tools/databricks/databricks.exe model-versions get-by-alias sentinelops_dev.sentinelops_dev.turbofan_rul champion -o json
```

3. Check posted costs before any compute. Billing lags about 9 hours, so a
   low number isn't proof of low spend. The API sometimes returns 429;
   retry after a few minutes.
   - September 24 (UTC) was projected at about CAD 13.4. The user approved
     going over CAD 10 against Azure credits that expire **October 10,
     2026**.
   - Record the posted figures for September 24 and 25 in STATUS ("Cost and
     runtime controls").

```powershell
az rest --method post --url 'https://management.azure.com/subscriptions/b1026367-46bf-43e0-93b5-bbfcc45a2291/providers/Microsoft.CostManagement/query?api-version=2023-03-01' --body '@infra/cost-query.json' --query properties.rows -o json
```

## 2. Authorization and non-negotiable constraints

- **Account and budget:**
  - The user authorized Azure work under `cheng.huang.ca@outlook.com`, with
    a guide of **up to $10/day**, treated as CAD (the conservative reading).
  - They have Azure credits until **October 10, 2026**, so an approved task
    needn't wait for the next UTC day. Still state the cost up front and ask
    for each billable resource.
  - There is no hard cutoff: budget `sentinelops-dev-monthly` only emails.
- **Known costs:**
  - About CAD 1.7/day is fixed: the managed resource group's NAT gateway and
    public IP bill 24/7.
  - Serverless jobs cost ~CAD 0.62/DBU, about 1.5 DBU per hour of job time.
  - Serverless SQL costs CAD 0.97/DBU. The 2X-Small warehouse is 4 DBU/h,
    and every wake-up costs at least ~CAD 0.35: dashboards, Genie, Genie
    space creation, and Catalog Explorer sample data all wake it.
  - CPU model serving costs CAD 0.097/DBU, 1 DBU/h per unit of provisioned
    concurrency.
  - Pay-per-token model calls cost cents.
- **No Vector Search endpoint** (the user's choice: ~CAD 9.3/day, plus 24 h
  of billing after the last index is deleted). Anything billed by the hour
  (serving endpoints, Event Hubs, warehouses) is a bounded demo, deleted
  afterwards, with the user's go-ahead.
- **Ask first** before:
  - downloading files, creating billable or long-lived resources;
  - changing permissions, grants, principals or secrets;
  - deleting anything, or uploading to landing.
- **Git:** commit only when the user asks. There is **no Git remote**; don't
  create one or push without the user's choice of repository and visibility.
- Don't modify the unrelated `rg-equity-silver-mlops` resources or the idle
  `kinesis_ingestion` pipeline. No credentials in the repo; use the existing
  Azure CLI login.
- **ML rules:**
  - split by engine;
  - never tune on the official test labels, and don't pick models by test
    RMSE;
  - promote only through `cmapss_promote`;
  - serving and scoring use Spark-computed Gold features, never
    pandas-recomputed ones.
- **OSHA data:**
  - attribute it to the U.S. Department of Labor, with no implied
    endorsement;
  - never use the assistant to identify workers or employers;
  - `data/osha` (the raw archive, with employer names) stays local: never
    upload or commit it, and never print names from it;
  - landing files are immutable; a correction is a new landing version with
    its own append flow (see masking v2).
- **Evaluation discipline:**
  - tune only on DEV questions; a held-out set runs once, in the job;
  - a used set becomes a regression set, and a new held-out set is a new
    list (never edit a used one);
  - run `scripts/scan_answer_names.py` on every answer-eval report before
    trusting identity declines.

## 3. Remaining work (the roadmap)

In the recommended order. For each task: check official docs and current
prices first, state the cost, get the approvals listed, rehearse locally,
then run in the cloud. Finish with the checklist in section 7.

| # | Task | Approval needed | Cost character |
|---|---|---|---|
| A | REST API ingestion (**next**) | Yes: the source, and the download | Serverless job minutes; the API is free |
| B | Event Hubs (Kafka endpoint) streaming demo | Yes: namespace (billable), secret scope, new SDK dependency | Hourly namespace cost; delete the same day |
| C | Environments and CI/CD (staging/prod, service principals, GitHub, OIDC) | Yes: repository and visibility, catalogs, principals, grants | Mostly free |
| D | Optional: agent deployment and review app | Yes: serving endpoint | Serving while scaled up, plus tokens |
| E | Optional ML depth: FD002–FD004, tuning, `mlflow.evaluate`, sequence baseline, Lakehouse Monitoring | Landing upload (FD002–4); monitoring (billable) | Serverless minutes; monitoring has a 2× DBU multiplier |
| F | Small follow-ups (below) | Varies | Cents |
| G | Demo script and portfolio write-up (last) | Publishing externally: yes | Free |

### A. REST API ingestion (next)

**Goal:** a third ingestion style (API → immutable landing → Auto Loader →
medallion), next to files (C-MAPSS) and, later, streaming (Event Hubs).

1. **Choose a source with the user.**
   - Prefer keyless APIs with a clear license. Suggestion: Open-Meteo's
     historical weather archive (keyless; CC BY 4.0, attribution required;
     check the current terms and rate limits). Daily maximum temperature for
     one representative city per state could later be joined to OSHA
     heat-illness reports by state and month.
   - A keyed API (for example EIA) needs a secret scope; see task B.
2. **Fetch job** (`jobs/fetch_<source>.py`, manual bundle job, same
   guardrails as the others).
   - Request a bounded, parameterized window.
   - Write each **raw** response unchanged to
     `/Volumes/.../landing/<source>/v1/...json`, with deterministic file
     names from the request parameters, and never overwrite: skip if present.
   - Log request counts and bytes. Serverless jobs already reach PyPI, so
     egress works; confirm the API is reachable.
3. **Pipeline:** Auto Loader JSON with an explicit schema and
   `_rescued_data` into Bronze; typed, deduplicated Silver with
   expectations and quarantine; a Gold mart. Follow `pipelines/osha.py` and
   `pipelines/medallion.py`.
4. **Tests:** file naming and immutability, response parsing, the schema.
   Rehearse locally on one saved response before any cloud run.
5. **Evidence:** a first run, then a rerun that appends 0 rows (a
   checkpoint proof), with the pipeline update summarized by
   `scripts/pipeline_update_evidence.py` into `docs/`. Optionally, a
   dashboard tile or Genie table.

### B. Event Hubs streaming demo (bounded)

1. **Check prices** (Event Hubs Standard throughput unit per hour, plus
   ingress). Standard is the lowest tier with the Kafka endpoint.
2. **With approval:** create a namespace (Standard, 1 TU, `westus2`,
   `rg-sentinelops-dev`), one hub (1–2 partitions), and a listen and a send
   SAS policy.
   - Put the listen connection string in a **Databricks-backed secret
     scope** (`databricks secrets create-scope` / `put-secret`), never in the
     repo.
   - Delete the namespace **the same day** and confirm in the inventory.
3. **Producer (local):** replay C-MAPSS FD001 **test** rows (local
   `data/cmapss`) as JSON events. This needs a Kafka or Event Hubs client
   library, which is a new dependency: ask.
4. **Consumer:**
   - A pipeline streaming table reading Kafka with `SASL_SSL` / `PLAIN`,
     user `$ConnectionString`, the secret as password, port 9093, into Bronze
     `cmapss_stream_events`.
   - Triggered, not continuous.
   - Check the current docs for Kafka support in serverless pipelines, and
     for reading secrets there.
5. **Evidence:** events sent = rows landed; a rerun appends 0.

### C. Environments and CI/CD

- **Ask the user for:**
  - the GitHub repository and visibility;
  - staging and prod catalogs (for example `sentinelops_staging`,
    `sentinelops_prod`) on the same metastore and storage;
  - service principals for `run_as`;
  - the grants. These are permission changes.
- **Bundle:**
  - add `staging` and `prod` targets (`mode: production`, `run_as`, catalog
    variable, workspace root);
  - everything already reads `${var.catalog}`: dashboards via
    `dataset_catalog`, and the Genie space via inline YAML;
  - `infra/serving-endpoint.json` hard-codes `sentinelops_dev`, so
    parameterize it if serving moves.
- **CI:** `.github/workflows/ci.yml` (pytest) has never run. Add `bundle
  validate` and deploys with GitHub OIDC → Databricks workload identity
  federation, with no long-lived tokens:
  - pull request: test and validate;
  - `main`: deploy to staging;
  - tag or approval: deploy to prod.
- **Data:** the staging and prod catalogs start empty. Decide with the user
  whether to re-land data there, or keep staging and prod as deploy-only
  proofs.

### D. Optional: agent deployment and review app

The masking gap no longer blocks this; masking v2 is verified.

- **Wrap `sentinelops.answers.Assistant`** as an MLflow `ResponsesAgent`.
  Endpoints can't read Delta without a warehouse, so package the 256-dim
  index and document text as model artifacts (about 104 MB of vectors,
  roughly 150–200 MB in total; fits the 4 GB CPU limit).
- **Declare the embedding and chat endpoints as resources**, for automatic
  authentication passthrough.
- **Deploy** with scale-to-zero and a review app (check the current Agent
  Framework and `databricks-agents` docs and prices). Rerun the
  identity and v2 sets against the endpoint as a regression, then delete
  it.
- **Consider a second layer:** decline any draft that tries to name a
  masked `[EMPLOYER]`. The model answered 3 of 16 identity requests, but
  without names.

### E. Optional ML depth

- **FD002–FD004:**
  - they're already in `data/cmapss/CMAPSSData.zip`;
  - `sentinelops.landing` is FD001-only, the Silver endpoint-label
    expectation fails any row that isn't FD001 (`pipelines/medallion.py`),
    and `verify` asserts FD001's exact counts; all three need generalizing;
  - add condition-aware normalization (6 operating conditions);
  - land as a new version and ingest through a new append flow (the masking
    v2 pattern), with approval.
- **Tuning:** engine-grouped CV on training engines only (a new
  dependency, such as Optuna, needs asking). Log with MLflow and promote
  only through the gate.
- **`mlflow.evaluate`:** on validation engines, with the NASA score as a
  custom metric.
- **Sequence baseline:** optional; torch is a heavy dependency.
- **Lakehouse Monitoring** on `gold.cmapss_predictions`: billed at 2× DBUs
  plus warehouse time for its dashboard; ask. The job-computed metrics
  already cover the demo.

### F. Small follow-ups

- Add `analytics_refresh` as the last task of `cmapss_retrain`, so
  `cmapss_fleet_status` can't go stale after a promotion.
- Ask an account or metastore admin for read access to `system.billing`, for
  a same-day usage view.
- The eval v2 misses: the injection-injury retrieval miss (few matching
  narratives), and the robbery answer key (assaults vs shootings).
- A masking review of residual initials ("G&H [EMPLOYER]") and contractor
  names; any fix is a new landing version.
- Genie: a larger held-out question set, or Genie `benchmarks` (uses
  warehouse time).
- Screenshots for the write-up. The in-app browser can't save them; ask the
  user, or use Claude in Chrome if it's connected.
- The two old diagnostics in `/Workspace/Users/cheng.huang.ca@outlook.com/sentinelops-scratch`
  are safe to delete, with the user's OK.

### G. Demo script and portfolio write-up (last)

- Follow `SentinelOps.md` ("Demo script", 10 minutes):
  1. UC lineage;
  2. pipeline expectations;
  3. MLflow champion/challenger;
  4. serving: recreate the endpoint from `infra/serving-endpoint.json` for
     the demo, then delete it;
  5. drift and alerts (dashboard);
  6. an assistant question with its trace;
  7. Genie.
- Use the evidence files under `docs/`, and state the honest caveats from
  STATUS, SAFETY_RAG and ANALYTICS.
- Publishing anything externally needs the user's approval.

### What exists (reuse, don't rebuild)

- **C-MAPSS:**
  - `cmapss_retrain` runs ingest → verify → train (only when Gold training
    digests change; `force_retrain` overrides) → promote (only an undecided
    `@challenger`) → score → monitor → alerts. Checks are logged to
    `gold.cmapss_alerts`.
  - `verify` asserts the benchmark's exact counts.
- **Serving:** `infra/serving-endpoint.json` defines the endpoint (Small
  CPU, scale-to-zero, inference table). `cmapss_serving_check` proves
  bit-identical parity, measures latency and checks the inference table;
  its 3-minute wait is too short, so check the table later.
- **OSHA pipeline:** the default flow reads `osha_sir/v1`, and each prefix in
  `sentinelops.osha_landing_later` gets an append flow (`osha_sir_v2`).
  `python -m sentinelops.osha` writes the next landing version locally (now
  `osha_v2`, masking v2).
- **GenAI:**
  - `sentinelops.answers` / `answer_eval` (job `osha_answer_eval`,
    `--question-sets`): the traced `Assistant`, `ChatClient` and
    `evaluate_assistant`, with per-set results and decline routes.
  - Sets: `eval_v3_identity` (last held out), plus `eval_v2` and `eval_v1`
    (regression).
  - `sentinelops.extraction` (job `osha_extraction_eval`).
  - `scripts/scan_answer_names.py` scans reports for employer names,
    locally.
- **Analytics:** `analytics_refresh` rebuilds `gold.osha_injury_facts` and
  `gold.cmapss_fleet_status`, adds comments for Genie, and runs every
  dashboard dataset and Genie example query. Edit
  `dashboards/*.lvdash.json` directly, then run the tests, the job, and a
  render check (the user signs in to the in-app browser).

## 4. Lessons already paid for — don't relearn them

| Symptom or trap | What to do |
|---|---|
| Serverless Python tasks have no `__file__` | Pass `--source-root ${workspace.file_path}/src` and put it on `sys.path` (all jobs do this) |
| skops refuses to load the model | Trust only `...hist_gradient_boosting.predictor.TreePredictor` (`registry.py`); never broaden it automatically |
| Spark vs pandas rolling means differ by ~1e-12 | They move gradient-boosting results (v3 RMSE 18.34 vs v2 18.16). Serve and score from Gold Spark features |
| `cycle` is both a Gold key and a model feature | `select(*KEYS, *FEATURES)` repeats it and shifts every value after it. The serving endpoint scored such rows without error. Select features without the keys; `serving.request_body` rejects duplicate columns |
| Direct REST calls to the embedding endpoint return 429 | This workspace throttles by **input count** (16/request OK, 32 rejected; ~24 inputs/s). Use `ai_query` for bulk work (~470 docs/s) |
| The SDK's `api_client.do` looks hung | It silently retries 429s for ~5 minutes. Use plain `requests` with `WorkspaceClient().config.authenticate()` headers |
| Judging job progress from UC table properties | They didn't reflect MERGE commits. Count rows or log progress instead |
| `bundle run` output and CLI JSON in PowerShell 5.1 | Decode with `utf-8-sig` (BOM); `ConvertFrom-Json` mishandles arrays, so pipe to Python |
| Inline Python in PowerShell 5.1 loses its double quotes; Git Bash heredocs can mangle `\b` | Put the Python in a file and run it; write JSON request bodies to a file and pass `@file` |
| Git Bash mangles `/subscriptions/...` arguments to native tools | Run Databricks/Azure CLI commands from PowerShell |
| `pipelines list-pipeline-events` lacks details | Use `databricks api get "/api/2.0/pipelines/<id>/events?max_results=250"`, then `scripts/pipeline_update_evidence.py` |
| STANDARD jobs can wait 3–7 minutes for resources | Ingest runs take 11–12 of their 15 minutes; if one times out while waiting, rerun it before raising the limit |
| Bundle dev mode forces development pipelines | Keep `targets.dev.presets.pipelines_development: false` |
| Every deploy "updates" three jobs | `cmapss_ingest`, `osha_ingest` and `cmapss_retrain` resend identical settings (the API doesn't echo `disable_auto_optimization` on pipeline tasks); harmless. `bundle deploy --select <resource>` deploys one resource |
| Serverless tasks retried despite `max_retries: 0` | Serverless auto-optimization retries failed tasks. Every task sets `disable_auto_optimization: true`; `tests/test_bundle.py` enforces it |
| If/else conditions need task values | Task values can only be set from notebooks, whose environment differs. `cmapss_retrain` uses self-deciding steps (`--only-if-changed`, `--only-pending`) |
| Running one task of a job | `jobs run-now --json @file` with `"only": ["task"]`; other tasks show `DISABLED`, and a failure reads `INTERNAL_ERROR`/`FAILED` |
| Adding a source to an existing streaming table | Add `@dp.append_flow(target=..., name=...)`; don't change an existing flow's path. Flow names identify checkpoints: never rename or reuse one |
| Paid model calls re-executed by a second read | Write `ai_query` results once to a staging table (see `jobs/embed_osha.py`) |
| Quick experiments in the cloud | A one-off `databricks jobs submit` with a serverless environment ran in ~1 minute |
| Python `write_text` on Windows writes CRLF | The repo is LF. Write bytes, or normalize before committing. Check with `tr -cd '\r' < file | wc -c` |
| Answer-key bugs look like model errors | Inspect retrieved documents before blaming retrieval (OSHA's reversed "Stationary saws  table" title; robbery assaults vs shootings) |
| GPT-OSS content and citations | `message.content` is a list of `reasoning` + `text` blocks; it sometimes cites as `【id】`. `sentinelops.answers` handles both |
| LLM prompts that "describe the sample" still count it | The prompt declines numbers, frequencies, shares, rankings and trends explicitly |
| Llama judge literalism | It reads "A, B or C" as all required and matches decline wording. Use single general facts and a generic expected decline; read judge scores for answered rows only |
| Judge and model outputs vary between runs | v1's conveyor judge false negative didn't recur. One run per set; no confidence intervals |
| `mlflow.genai.evaluate` makes an extra paid call | `evaluate_assistant` sets `MLFLOW_GENAI_EVAL_SKIP_TRACE_VALIDATION` |
| Local MLflow with a SQLite store writes `./mlruns` | Create the experiment with an explicit `artifact_location`, or run from the scratchpad |
| Keyword share overstates question support | Read matching narratives before writing a question |
| Threshold vs model declines | Incident-style questions and in-domain injections score above the threshold. The model's decline is the real defense, so test it with such questions |
| Employer names in narratives | Full-name masking missed shortened names (~0.1%), and the assistant repeated one. Masking v2 masks proper-noun variants only (case-insensitive masking over-masked ~290–1,100 ordinary words). Validate any scanner against a known leak: the first version missed a business word + ordinary word name |
| Cost Management query returns HTTP 429 | It throttles bursts; retry after a few minutes |
| OSHA codes aren't comparable across 2024 | Score and chart harmonized divisions (`sentinelops.extraction.truth`), never raw code prefixes |
| Chat endpoints return 429 to parallel REST calls | Use 2 workers locally; `ai_query` batched 1,051 extractions with 0 failures |
| GPT-OSS-20B returns empty content | At medium effort it can spend all `max_tokens` on reasoning; prefer 120B |
| Unexplained serverless SQL cost | Catalog Explorer sample data starts the warehouse. `w.query_history.list` (`client_application`) shows who started it |
| Genie space deploy fails with 403 "Table ... does not exist" | Its tables must exist first: deploy, run `analytics_refresh`, then deploy again. Creating or updating a space starts the warehouse |
| AI/BI table shows "Visualization has no fields selected" | Use table spec version 2 (`fieldName` + `displayName` per column) |
| Chart series share colors | The renderer cycles 10 colors; keep at most 10 series (a test checks the color columns) |
| Dashboard and Genie SQL bugs | Check them in `analytics_refresh` on serverless job compute, not on the warehouse |
| Genie summary numbers | It miscounted 43 listed rows (21 vs 20). Give example SQL that aggregates, and check its SQL results, not its prose |
| Serving endpoint provisioning | ~10 minutes from create to READY (container build). Scale-to-zero stops billing after 30 idle minutes |
| AI Gateway inference table on a CPU custom model | Standard delivery took 5–40 minutes (no `_otel_logs`). Check it later, not in the same job |
| Browser checks of the workspace | The in-app browser needs the user to sign in, and it can't save screenshots or zoom. Claude in Chrome was not connected on September 24 |

## 5. Resources

Subscription `b1026367-46bf-43e0-93b5-bbfcc45a2291` (Azure subscription 1),
region `westus2`. Resource groups `rg-sentinelops-dev` and
`rg-sentinelops-dev-managed`. Workspace `dbw-sentinelops-dev`,
https://adb-7405619144539463.3.azuredatabricks.net (ID `7405619144539463`).
ADLS `stsent7s5fwynthfd64` (`landing`, `bronze`, `silver`, `gold`,
`metastore`). Access connector `ac-sentinelops-dev`, UC credential
`sentinelops_adls`, external location `sentinelops_metastore`, catalog
`sentinelops_dev` (bound to this workspace; predictive optimization off).
Infrastructure is in `infra/main.bicep`, the UC setup in `infra/uc-*.json`,
the budget in `infra/budget.json`, and the demo endpoint in
`infra/serving-endpoint.json`. Bundle `sentinelops`, target `dev`.

| Bundle resource | ID | Purpose / latest evidence |
|---|---|---|
| pipeline `cmapss_medallion` | `77ecd502-9283-4528-83e3-7ab8666ada1e` | C-MAPSS Bronze→Silver→Gold |
| job `cmapss_ingest` / `cmapss_verify` | `264959928637120` / `366011234786265` | Run the pipeline / independent parity checks |
| job `cmapss_train` / `cmapss_promote` / `cmapss_score` | `477632929595835` / `714826690927619` / `362250970463849` | Train → gate → score + monitor |
| job `cmapss_retrain` | `1037945637149770` | Orchestrated loop (run `599725542930474`; breach test `955572570273055`) |
| job `cmapss_serving_check` | `782318629064026` | Serving parity (run `1018787913287976`) |
| job `fd001_baseline` | `446848359557450` | Original bootstrap (kept) |
| pipeline `osha_safety` | `7d53a0fb-d724-4628-a854-23dc1d0e283a` | OSHA Bronze→Silver→Gold; v2 update `5261ee72…` |
| job `osha_ingest` / `osha_embed` | `1070808576153729` / `379147303783128` | Ingest (run `929857370324250`) / embed (run `771297237993579`) |
| job `osha_retrieval_eval` | `383639217735446` | Retrieval evaluation (run `603274434690806`) |
| job `osha_answer_eval` | `1029765841933443` | v1 run `745084593826476`; v2 run `425541794390409`; identity run `731577238433197` |
| job `osha_extraction_eval` | `804198192778755` | Extraction (run `570236144351626`) |
| job `analytics_refresh` | `847239470140874` | Marts + SQL checks (run `429694746857912`) |
| dashboards `fleet_health` / `safety_incidents` | `01f1b83350951effa1d1f1bc6ca9e6cd` / `01f1b83350861a42888ebab34d8a8785` | Published, viewer credentials |
| genie space `sentinelops_operations` | `01f1b8347de912dc8d94fcb07a9144ec` | 6 curated Gold tables |

- All jobs are manual, STANDARD, one concurrent run, zero retries (including
  serverless auto-optimization), with timeouts and no schedules.
- Pipelines are triggered serverless, with development mode off.
- The serving endpoint `sentinelops-rul-demo` was deleted on September 24.
  Its inference table, `sentinelops_dev.sentinelops_dev.turbofan_rul_demo_payload`,
  is kept.
- **Model:** `sentinelops_dev.sentinelops_dev.turbofan_rul`, **v3
  `@champion`** (Gold-trained); v2 bootstrap, no alias.
- **MLflow experiments:**
  - `sentinelops-fd001` `2331823695746663`;
  - `sentinelops-safety-rag` `3423406210325716`: retrieval evals, answer
    evals v1 `17406be7…`, v2 `cf0414db…` and identity `11223ab6…`, and
    extraction `840d418d…`.
- **Landing volume** `sentinelops_dev.sentinelops_dev.landing`:
  - `cmapss/` (bootstrap);
  - `cmapss_ingest/v1` (includes the permanent quality probe);
  - `osha_sir/v1` (masking v1);
  - `osha_sir/v2` (masking v2; its copies win in Silver).
- **Tables:**
  - `bronze.{cmapss_lines, cmapss_labels, osha_sir_reports}`
  - `silver.{cmapss_observations, cmapss_quarantine, cmapss_conflicts, cmapss_endpoint_labels, osha_incidents, osha_quarantine}`
  - `gold.{cmapss_features, cmapss_training_labels, cmapss_test_endpoints, cmapss_predictions, cmapss_model_performance, cmapss_feature_drift, cmapss_alerts, cmapss_fleet_status, osha_documents, osha_embeddings, osha_extractions, osha_injury_facts}`
  - bootstrap `sentinelops_dev.{silver_fd001_train, gold_fd001_features, gold_fd001_predictions}`

## 6. Local workspace and tools

- Directory: `C:\Users\cheng\Local Documents\Teck\Azure Databricks Project`.
  Shell: PowerShell; Git Bash is also available. Python 3.12 in `.venv`.
- Databricks CLI 1.17.0 at `.tools/databricks/databricks.exe` (SHA-256
  verified); dot-source `scripts/Use-SentinelOps.ps1` first. The bundle uses
  the direct deployment engine (required for Genie spaces).
- **Git-ignored local data:**
  - `data/cmapss` (the NASA archive, all four subsets);
  - `data/osha` (**the raw OSHA archive with employer and address data;
    never upload, commit or print names from it**);
  - `data/landing/*` (prepared landing files: `v1`, `osha_v1`, `osha_v2`);
  - `artifacts/`.
- **Pinned cloud dependencies** (serverless environment 4 / Python 3.12):
  numpy 2.5.3, pandas 2.3.3, scikit-learn 1.9.1, MLflow 3.16.1, skops 0.15.0,
  databricks-sdk 0.140.0, PyYAML 6.0.3. Keep local and cloud versions
  consistent.
- **Git:** branch `main`, author Cheng Huang <cheng.huang.ca@outlook.com>
  (repo-local config), no remote.
  - Recent milestones: `b94c34a` dashboards/Genie/budget, `d8bfec0`
    serving, `9c1b93e` eval v2, `7a863dd` masking v2.
  - The CI workflow `.github/workflows/ci.yml` has never run.

## 7. Working agreement that has served well

- Check official docs and prices before each milestone; state costs up front
  and ask for the approvals in section 3.
- Rehearse locally on real data before spending cloud minutes, including the
  job's own frame handling (the `cycle` bug), and run small diagnostics
  before long jobs.
- Record evidence (run IDs, JSON under `docs/`), including mistakes and label
  limitations. Redact anything identifying.
- For anything judged by a model or a label set, keep a dev set for tuning
  and a held-out set that is run once. Fix the answer key before blaming the
  model.
- Watch cloud runs with a background monitor that reports each state change,
  and read the last JSON line each job prints.
- End every milestone with:
  - tests;
  - `bundle validate --strict` and `bundle plan`;
  - a compute inventory, confirming nothing is left running (warehouse
    STOPPED, no custom endpoints);
  - a cost check;
  - STATUS task table and at-a-glance, README and HANDOVER updates.

  Then offer to commit.
