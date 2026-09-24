# SentinelOps — handover for the next session

Last updated: September 24, 2026, 07:50 UTC. Git `main` is clean; the
latest commit is the structured-extraction milestone (see section 6).

**Where things stand.** Both halves of the portfolio project run in Azure
Databricks.

- **Predictive maintenance** (NASA C-MAPSS FD001):
  - Auto Loader/Lakeflow ingestion into Bronze/Silver/Gold.
  - Training from Gold, with a validation-gated champion (model version 3).
  - Idempotent fleet batch scoring, with delayed-label and drift monitoring.
- **Safety GenAI assistant** (OSHA Severe Injury Reports):
  - Privacy-minimized ingestion into Gold documents.
  - Qwen3 embeddings for all 105,993 documents.
  - Exact retrieval, validated against OSHA-code relevance.
  - Grounded answers (GPT-OSS-120B) with code-checked `[report_id]`
    citations, a calibrated decline rule and MLflow tracing. On 28 held-out
    questions: 28/28 correct decisions; the Llama 3.3 judge passed correctness
    11/12 and groundedness 12/12.
  - Structured extraction (event, nature, body part, source) scored against
    harmonized OSHA codes. GPT-OSS-120B matches a supervised TF-IDF model on
    three fields but trails on source (0.760 vs 0.825).
- **The next task is orchestrated retraining and alerts** (RUL side).

The authoritative task tracker is the **Task status** table at the top of
[docs/STATUS.md](docs/STATUS.md). Update it as work lands.

## 1. Start here

1. Read, in this order:
   - this file;
   - `docs/STATUS.md` (task table + current milestone);
   - `docs/SAFETY_RAG.md` (design, measurements, retrieval, answer and extraction evaluations);
   - `docs/OPERATIONS.md` and `docs/INGESTION.md` (needed for the next task);
   - `SentinelOps.md` for the full intended scope.
2. Verify the environment and that nothing is running (all free):

```powershell
. ./scripts/Use-SentinelOps.ps1
az account show --query '{name:name,id:id,user:user.name}' -o json
.venv/Scripts/python.exe -m pytest -q                      # expect 64 passed
.tools/databricks/databricks.exe bundle validate --strict -t dev
.tools/databricks/databricks.exe jobs list-runs --active-only -o json
.tools/databricks/databricks.exe clusters list -o json
.tools/databricks/databricks.exe warehouses list -o json   # starter warehouse should be STOPPED
.tools/databricks/databricks.exe vector-search-endpoints list-endpoints -o json   # expect none
.tools/databricks/databricks.exe model-versions get-by-alias sentinelops_dev.sentinelops_dev.turbofan_rul champion -o json
```

3. Check posted costs before any compute. Billing lags about 9 hours, and
   an empty or low number is **not** proof of low spend:

```powershell
az rest --method post --url 'https://management.azure.com/subscriptions/b1026367-46bf-43e0-93b5-bbfcc45a2291/providers/Microsoft.CostManagement/query?api-version=2023-03-01' --body '@infra/cost-query.json' --query properties.rows -o json
```

## 2. Authorization and non-negotiable constraints

- The user authorized Azure work under `cheng.huang.ca@outlook.com` with a
  budget of **up to $10/day** for the whole demo. No hard cutoff or budget alert
  exists. About CAD 1.7/day is fixed (the managed resource group's NAT gateway
  and public IP bill 24/7). Serverless jobs cost ~CAD 0.62/DBU, roughly 1.5 DBU
  per hour of job time. Pay-per-token model calls are cheap (see SAFETY_RAG.md).
- **No Vector Search endpoint.** The user chose exact retrieval, because a
  Standard endpoint costs ~CAD 9.3/day and bills for 24 h after the last index
  is deleted. Don't leave serving endpoints, Event Hubs or SQL compute running.
  Anything billed by the hour is a bounded demo, deleted afterwards, and needs
  the user's go-ahead.
- Ask before downloading files, creating billable or long-lived resources,
  changing permissions or grants, or deleting anything. Commit only when the
  user asks. There is **no Git remote**; don't create one or push without the
  user's choice of repository and visibility.
- Don't modify the unrelated `rg-equity-silver-mlops` resources. No
  credentials are stored in the repo; use the existing Azure CLI login.
- Model evaluation: split by engine, never tune on the official test labels,
  don't pick models by test RMSE, and promote only through `cmapss_promote`.
  Serving and scoring must use Spark-computed Gold features, not
  pandas-recomputed ones.
- OSHA data: attribute it to the U.S. Department of Labor with no implied
  endorsement. Never use the assistant to identify individuals or employers.
  Landing files are immutable; don't re-upload the quality probe or OSHA files.

## 3. Next task in detail: orchestrated retraining and alerts

The GenAI side is complete up to deployment, so the recommended order returns
to the RUL side. Read `docs/OPERATIONS.md` and `docs/INGESTION.md` first.

1. **One manual multi-task job** chaining the existing steps: ingest
   (pipeline task on `cmapss_medallion`) → verify → train → promote → score →
   monitor. Use `depends_on`; keep the task code and parameters of the current
   jobs, and keep those jobs for single-step use. Follow the usual guardrails
   (STANDARD, one concurrent run, zero retries, task and job timeouts, no
   schedule); `tests/test_bundle.py` enforces them. Running as one job also
   prevents concurrent Gold reads during a refresh.
   - `cmapss_promote` records a rejection as a tag and exits 0; it fails only
     when no `@challenger` exists. Scoring then continues with the current
     `@champion`, which is the intended behavior.
   - A full chain is roughly 40 minutes of serverless (ingest ~12, verify ~6,
     train ~6.5, promote ~6.4, score/monitor ~9.3), ≈ CAD 0.6. State that
     before running.
2. **Alerts without a SQL warehouse.** Add a final task that reads the newest
   snapshots in `gold.cmapss_model_performance` and `gold.cmapss_feature_drift`
   and fails when thresholds are breached. Set the thresholds in advance,
   relative to each model version's first snapshot (for example, endpoint RMSE
   up more than 20%, or any `psi_age_matched` > 0.2), never from test RMSE.
   Job email notifications on failure would make it an alert; **ask the user**
   before configuring notifications or an address. Databricks SQL alerts would
   need warehouse time while evaluating.
3. Rehearse locally, then run the chain once end to end. Exercise the alert
   path with a deliberately impossible threshold in a separate run, and record
   the evidence under `docs/`.

**What exists on the GenAI side** (reuse, don't rebuild):
- `sentinelops.answers` / `answer_eval` and job `osha_answer_eval`: grounded
  answers, code-checked citations, a calibrated decline rule, MLflow traces,
  and the held-out evaluation (`docs/osha-answer-eval.json`). Before any
  deployment, run a larger evaluation with more in-domain unanswerable
  questions near the 0.6511 threshold.
- `sentinelops.extraction` and job `osha_extraction_eval`: harmonized OIICS
  truth (OSHA changed codes in 2024), a JSON-schema prompt (v3), validation and
  scoring. Raw outputs are in `gold.osha_extractions`; evidence is in
  `docs/osha-extraction-eval.json`.

## 4. Lessons already paid for — don't relearn them

| Symptom or trap | What to do |
|---|---|
| Serverless Python tasks have no `__file__` | Pass `--source-root ${workspace.file_path}/src` and put it on `sys.path` (all jobs do this) |
| skops refuses to load the model | Trust only `...hist_gradient_boosting.predictor.TreePredictor` (`registry.py`); never broaden it automatically |
| Spark vs pandas rolling means differ by ~1e-12 | They move gradient-boosting results (v3 RMSE 18.34 vs v2 18.16). Serve and score from Gold Spark features |
| Direct REST calls to the embedding endpoint return 429 | This workspace throttles by **input count** (16/request OK, 32 rejected; ~24 inputs/s). Use `ai_query` for bulk work (~470 docs/s) |
| The SDK's `api_client.do` looks hung | It silently retries 429s for ~5 minutes. Use plain `requests` with `WorkspaceClient().config.authenticate()` headers to see status codes |
| Judging job progress from UC table properties | Those properties didn't reflect MERGE commits, and a healthy run was cancelled because of it. Count rows or log progress instead |
| `bundle run` output and CLI JSON in PowerShell 5.1 | Decode with `utf-8-sig` (BOM); `ConvertFrom-Json` mishandles arrays, so pipe to Python instead |
| Git Bash mangles `/subscriptions/...` arguments to native tools | Run Databricks/Azure CLI commands from PowerShell |
| `pipelines list-pipeline-events` lacks details | Use `databricks api get "/api/2.0/pipelines/<id>/events?max_results=250"`, then `scripts/pipeline_update_evidence.py` |
| STANDARD jobs can wait ~7 minutes for resources | Ingest runs took 11–12 of their 15 minutes; if one times out while waiting, rerun it before raising the limit |
| Bundle dev mode forces development pipelines | Keep `targets.dev.presets.pipelines_development: false` |
| Paid model calls re-executed by a second read | Write `ai_query` results once to a staging table (see `jobs/embed_osha.py`) |
| Quick experiments in the cloud | A one-off `databricks jobs submit` with a serverless environment ran in ~1 minute; scripts go in `/Workspace/Users/cheng.huang.ca@outlook.com/sentinelops-scratch` (two old diagnostics there are safe to delete, with the user's OK) |
| Python `write_text` on Windows writes CRLF | The repo is LF. Write bytes, or normalize before committing |
| Answer-key bugs look like model errors | Inspect retrieved documents before blaming retrieval (eval v2 fixed OSHA's reversed "Stationary saws  table" title) |
| GPT-OSS content and citations | `message.content` is a list of `reasoning` + `text` blocks; it sometimes cites as `【id】`. `sentinelops.answers` handles both; IDs are still checked in code |
| LLM prompts that "describe the sample" still count it | GPT-OSS answered "how many … in 2022" by counting 2 retrieved reports until the prompt explicitly declined numbers, frequencies, shares, rankings and trends |
| Llama judge literalism | It treats "A, B or C" in expected facts as all required and matches decline wording. Use single general facts and a generic expected decline; read judge scores only for answered rows |
| `mlflow.genai.evaluate` makes an extra paid call | It calls `predict_fn` once more to validate tracing; `evaluate_assistant` sets `MLFLOW_GENAI_EVAL_SKIP_TRACE_VALIDATION` |
| Local MLflow with a SQLite store writes `./mlruns` | Create the experiment with an explicit `artifact_location` (tests use a temp dir) or run from the scratchpad |
| Keyword share overstates question support | 88 "grain bin/engulf" matches, but only one grain-engulfment narrative. Read matching narratives before writing a question |
| Cost Management query returns HTTP 429 | It throttles bursts; retry after a few minutes |
| OSHA codes aren't comparable across 2024 | OIICS codes and titles changed ("Fractures" 111 → 124; hips moved from trunk to lower extremities). Score on divisions harmonized by `sentinelops.extraction.truth`, never on raw code prefixes |
| Chat endpoints return 429 to parallel REST calls | GPT-OSS rejected 4 concurrent requests; use 2 locally. `ai_query` batched 1,051 extractions with 0 failures |
| GPT-OSS ignores a rule the schema order undercuts | It named the object tripped over in `source_object`, then categorized it. Put the rule inside the field the model writes first |
| GPT-OSS-20B returns empty content | At medium effort it can spend all `max_tokens` on reasoning (1.1% of test rows). Count these as invalid; prefer 120B |

## 5. Resources

Subscription `b1026367-46bf-43e0-93b5-bbfcc45a2291` (Azure subscription 1),
region `westus2`. Resource groups `rg-sentinelops-dev` and
`rg-sentinelops-dev-managed`. Workspace `dbw-sentinelops-dev`,
https://adb-7405619144539463.3.azuredatabricks.net (ID `7405619144539463`).
ADLS `stsent7s5fwynthfd64` (`landing`, `bronze`, `silver`, `gold`,
`metastore`). Access connector `ac-sentinelops-dev`, UC credential
`sentinelops_adls`, external location `sentinelops_metastore`, catalog
`sentinelops_dev` (bound to this workspace; predictive optimization off).
Infrastructure is in `infra/main.bicep`; the UC setup was done separately
with the CLI (`infra/uc-*.json`). Bundle `sentinelops`, target `dev`.

| Bundle resource | ID | Purpose |
|---|---|---|
| pipeline `cmapss_medallion` | `77ecd502-9283-4528-83e3-7ab8666ada1e` | C-MAPSS Bronze→Silver→Gold |
| job `cmapss_ingest` / `cmapss_verify` | `264959928637120` / `366011234786265` | Run the pipeline / independent parity checks |
| job `cmapss_train` | `477632929595835` | Train from Gold, register `@challenger` |
| job `cmapss_promote` | `714826690927619` | Validation gate → `@champion` |
| job `cmapss_score` | `362250970463849` | Score the fleet → monitor |
| job `fd001_baseline` | `446848359557450` | Original bootstrap (kept) |
| pipeline `osha_safety` | `7d53a0fb-d724-4628-a854-23dc1d0e283a` | OSHA Bronze→Silver→Gold |
| job `osha_ingest` / `osha_embed` / `osha_retrieval_eval` | `1070808576153729` / `379147303783128` / `383639217735446` | Ingest / embed / evaluate retrieval |
| job `osha_answer_eval` | `1029765841933443` | Grounded answers on held-out questions + Llama 3.3 judges (run `745084593826476`) |
| job `osha_extraction_eval` | `804198192778755` | Structured extraction vs OSHA codes and baselines (run `570236144351626`) |

All jobs are manual, STANDARD, one concurrent run, zero retries, with
timeouts and no schedules. Pipelines are triggered serverless with
development mode off.

- Model: `sentinelops_dev.sentinelops_dev.turbofan_rul` — **v3 `@champion`**
  (Gold-trained); v2 bootstrap, no alias.
- MLflow experiments: `sentinelops-fd001` `2331823695746663`,
  `sentinelops-safety-rag` `3423406210325716`. The latter holds retrieval
  eval v1/v2, answer eval run `17406be7875d4a2387785faf3ea9f083` (28
  traces, default experiment trace storage; UC trace tables would need a SQL
  warehouse) and extraction eval run `840d418d57f64dc894892467a09641f0`.
- An idle pipeline `kinesis_ingestion` exists in the workspace. It isn't part
  of this bundle; leave it alone unless the user says otherwise.
- Landing volume `sentinelops_dev.sentinelops_dev.landing`:
  - `cmapss/`: bootstrap;
  - `cmapss_ingest/v1`: includes the permanent quality probe;
  - `osha_sir/v1`: minimized OSHA JSONL plus manifest.
- Tables:
  - `bronze.{cmapss_lines, cmapss_labels, osha_sir_reports}`
  - `silver.{cmapss_observations, cmapss_quarantine, cmapss_conflicts, cmapss_endpoint_labels, osha_incidents, osha_quarantine}`
  - `gold.{cmapss_features, cmapss_training_labels, cmapss_test_endpoints, cmapss_predictions, cmapss_model_performance, cmapss_feature_drift, osha_documents, osha_embeddings, osha_extractions}`
  - bootstrap `sentinelops_dev.{silver_fd001_train, gold_fd001_features, gold_fd001_predictions}`

## 6. Local workspace and tools

- Directory: `C:\Users\cheng\Local Documents\Teck\Azure Databricks Project`.
- Shell: PowerShell; Git Bash is also available.
- Python 3.12 in `.venv`.
- Databricks CLI 1.17.0 at `.tools/databricks/databricks.exe` (SHA-256
  verified); dot-source `scripts/Use-SentinelOps.ps1` first.
- Git-ignored local data:
  - `data/cmapss` (NASA archive);
  - `data/osha` (**raw OSHA archive with employer and address data; never
    upload or commit it**);
  - `data/landing/*` (prepared landing files);
  - `artifacts/`.
- Pinned cloud dependencies (serverless environment version 4 / Python 3.12):
  numpy 2.5.3, pandas 2.3.3, scikit-learn 1.9.1, MLflow 3.16.1, skops 0.15.0,
  databricks-sdk 0.140.0. Keep local and cloud versions consistent.
- Git: branch `main`, author Cheng Huang <cheng.huang.ca@outlook.com>
  (repo-local config), 8 commits, no remote. The latest commit is the
  structured-extraction milestone. The CI workflow in
  `.github/workflows/ci.yml` has never run.

## 7. Working agreement that has served well

- Check official docs and prices before each milestone, and state costs up front.
- Rehearse locally on real data before spending cloud minutes. Run small
  diagnostics before long jobs.
- Record evidence (run IDs, JSON under `docs/`), including mistakes and label
  limitations.
- End every milestone with:
  - tests;
  - `bundle validate --strict`;
  - a compute inventory, confirming nothing is left running;
  - a cost check;
  - STATUS task-table, README and HANDOVER updates.

  Then offer to commit.
