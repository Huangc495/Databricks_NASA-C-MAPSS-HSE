# Portfolio-Quality Azure Databricks End-to-End Project: "SentinelOps" — Predictive Maintenance + Safety GenAI for Industrial Operations

## TL;DR
- Build **SentinelOps**, an end-to-end Azure Databricks project for an energy/industrial employer that (1) predicts equipment Remaining Useful Life (RUL) from NASA C-MAPSS turbofan sensor data, and (2) runs a Generative-AI RAG assistant over OSHA safety incident narratives — together covering every bullet of the target job description including the "safety and environmental" mandate.
- The architecture uses the current 2025–2026 Databricks stack: Unity Catalog governance, medallion Delta Lake with Auto Loader + Lakeflow Declarative Pipelines (formerly Delta Live Tables), MLflow experiment tracking + Unity Catalog model registry with champion/challenger aliases, Mosaic AI Model Serving with inference tables, Lakehouse Monitoring for drift, Mosaic AI Vector Search + Agent Framework for RAG, Databricks Asset Bundles for CI/CD, and AI/BI dashboards + Genie for business users.
- Achievable on the Azure 14-day Premium DBU trial plus a pay-as-you-go subscription (new Azure users get $200 credit for the first 30 days); expect roughly 40–60 hours of build time and modest Azure infrastructure cost if you use small serverless/single-node clusters and shut them down aggressively.

## Key Findings

**Domain fit.** The JD's emphasis on "courageous safety leadership" and "safety and environmental rules" strongly signals an energy / oil & gas / industrial / manufacturing employer. A two-pronged project is the strongest portfolio play: a **predictive-maintenance ML model** (reliability engineering) plus a **safety-incident GenAI assistant** (the explicit safety/environmental responsibility). This lets one project map cleanly to all six JD responsibility bullets.

**Primary ML dataset — NASA C-MAPSS Turbofan (RUL regression).** This is the canonical prognostics/health-management benchmark: four sub-datasets (FD001–FD004), each with `train_FD00X.txt`, `test_FD00X.txt`, `RUL_FD00X.txt`, 26 columns (unit number, time-in-cycles, 3 operational settings, 21 sensors), run-to-failure trajectories. Per the NASA C-MAPSS readme (A. Saxena & K. Goebel, 2008, NASA Prognostics Data Repository), the sub-datasets are: **FD001 = 100 train / 100 test** (1 operating condition, 1 fault mode: HPC degradation); **FD002 = 260 train / 259 test** (6 conditions, 1 fault); **FD003 = 100 / 100** (1 condition, 2 faults); **FD004 = 248 train / 249 test** (6 conditions, 2 faults — most complex). The task premise that C-MAPSS is "unavailable" is only partly true:
- The NASA **simulator software** (C-MAPSS/C-MAPSS40K) is marked non-public on data.gov, and the **PHM08 challenge variant** is unavailable on the NASA PCoE page.
- But the **core FD001–FD004 degradation dataset IS downloadable**. Best working sources: **Kaggle `behrad3d/nasa-cmaps`** (CC0 Public Domain, ~24.7 MB zip / 25,888,576 bytes); NASA PCoE S3 (`https://phm-datasets.s3.amazonaws.com/NASA/6.+Turbofan+Engine+Degradation+Simulation+Data+Set.zip`); and **Zenodo record 15346912** ("PCoE Turbofan Engine Degradation Simulation," `CMAPSSData.zip`, **12.4 MB, md5 `79a22f36e80606c69d0e9e4da5bb2b7a`, license CC-BY-4.0, DOI 10.5281/zenodo.15346912**).
- The old `data.nasa.gov` Socrata URL (ff5v-kuh6) now returns **404** — do not use it.
- A more advanced option: **N-CMAPSS (Turbofan Sim-2)**, HDF5 format, 8 sub-datasets DS01–DS08, real flight conditions (`https://phm-datasets.s3.amazonaws.com/NASA/17.+Turbofan+Engine+Degradation+Simulation+Data+Set+2.zip`). The Kaggle mirror `bishals098/nasa-cmapss-2-engine-degradation` reports the zip at **15,918,764,184 bytes (~15.9 GB), license "U.S. Government Works."**

**GenAI/RAG corpus — OSHA Severe Injury Reports (SIR).** Downloadable as a full CSV from the SIR Dashboard (`osha.gov/severe-injury-reports`); the dashboard page states the download covers **"Data from 1/1/2015 through 11/30/2025."** The federal OSHA SIR dataset now contains **105,991 severe injury reports from 70,589 employers** (per SafetyIncidents.org's tally of the federal record). It is U.S. federal public-domain data and crucially contains a free-text **`Final Narrative`** field (employer-written incident descriptions) plus structured OIICS fields (Nature, Part of Body, Event, Source, Primary_NAICS, EventDate, Employer, geocodes). The narrative is a genuine RAG corpus and also supports a "LLM extracts structured fields from unstructured reports" use case. It can also serve as a secondary ML task (incident severity/nature classification).

**Diverse sources for the "diverse data" bullet.** Combine: (a) batch file ingest of C-MAPSS text files via Auto Loader; (b) REST API pull (e.g., EIA energy data API or a public weather API) landed as JSON; (c) simulated streaming of sensor readings through Azure Event Hubs' Kafka-compatible endpoint consumed by Structured Streaming / Lakeflow. This demonstrates batch + API + streaming ingestion.

**Feature naming currency (2025–2026).** Delta Live Tables → **Lakeflow Declarative Pipelines** (existing `dlt` code still runs; new API is `from pyspark import pipelines as dp`, `@dp.table`, `@dp.materialized_view`, `AUTO CDC INTO` replacing `APPLY CHANGES INTO`); Databricks Workflows → **Lakeflow Jobs**; managed ingestion connectors → **Lakeflow Connect**. MLflow Model Registry stages (None/Staging/Production) are replaced by **Unity Catalog model registry with aliases** (champion/challenger). Legacy `databricks-feature-store` package superseded by **`databricks-feature-engineering`** (Feature Engineering in Unity Catalog). Databricks One → **Genie One**.

## Details

### 1. Business Problem Framing & Cross-Functional Collaboration

**Problem statement.** An industrial operator (turbomachinery fleet — turbofans/compressors/pumps) suffers unplanned downtime and safety incidents. Two linked problems: (a) reactive maintenance causes costly failures and safety exposure; (b) safety/EHS teams cannot quickly learn from a large corpus of past incident reports.

**Stakeholders & translation to data solutions:**
- **Reliability/Maintenance engineering** → RUL regression model to schedule maintenance before failure. Requirement "reduce unplanned downtime" → time-series feature engineering + RUL prediction + batch scoring feeding a work-order dashboard.
- **HSE / Safety leadership** (the JD's "courageous safety leader") → RAG assistant answering "What were the most common causes of amputation incidents in NAICS 3116?" grounded in OSHA narratives + regulations; LLM structured extraction to auto-code new incident reports.
- **Operations management** → AI/BI dashboard + Genie space for self-service KPIs.
- **Data platform/security** → Unity Catalog governance, secret scopes, private networking.

**KPIs & success metrics:**
- ML: RUL RMSE and the NASA PHM asymmetric scoring function (penalizes late predictions more than early); target RMSE materially below a naive baseline on FD001. Business KPI: % reduction in unplanned downtime, maintenance cost avoidance.
- GenAI: Agent Evaluation quality metrics (correctness, groundedness/faithfulness, relevance, safety) via LLM-as-judge; retrieval precision; latency and cost per query.
- Ops: pipeline freshness/SLA, data-quality expectation pass rate, drift alert lead time, model-refresh cadence.

### 2. Architecture on Azure Databricks

**Architecture diagram (described).** Left to right:
1. **Sources:** C-MAPSS text files (batch) → ADLS Gen2 landing container; REST API (EIA/weather JSON) → ADLS; simulated sensor stream → Azure Event Hubs (Kafka endpoint).
2. **Ingestion:** Auto Loader (`cloudFiles`) for files; Structured Streaming Kafka connector for Event Hubs; all landing in **Bronze** Delta tables.
3. **Medallion (Delta Lake + Lakeflow Declarative Pipelines):** Bronze (raw, appended, `_rescued_data`) → Silver (cleaned, typed, deduped, data-quality **expectations**) → Gold (feature/aggregate tables, RUL labels, incident marts).
4. **Governance:** **Unity Catalog** three-level namespace (`catalog.schema.table`) spanning dev/staging/prod catalogs; lineage, ACLs, audit logs; ADLS accessed through an **Access Connector managed identity** + external locations/storage credentials.
5. **ML:** Feature Engineering in UC → MLflow training/tuning → UC model registry (champion/challenger aliases) → Mosaic AI Model Serving + batch inference → inference tables.
6. **GenAI:** OSHA narratives chunked into Delta → Vector Search index → Agent Framework RAG chain calling a Foundation Model API → served endpoint + Review App.
7. **Monitoring:** Lakehouse Monitoring on inference tables (drift/quality) → SQL alerts → Lakeflow Jobs retraining trigger.
8. **Consumption:** AI/BI dashboards + Genie space.
9. **CI/CD:** Git + Databricks Asset Bundles + GitHub Actions deploying across dev→staging→prod targets.

**Why medallion + Lakeflow.** Lakeflow Declarative Pipelines let you declare tables in SQL/Python; the runtime manages dependency ordering, checkpoints, retries, autoscaling, schema evolution, and **expectations** (`EXPECT`, `EXPECT OR DROP`, `EXPECT OR FAIL`, quarantine) with a queryable event log — replacing hand-written checkpoint/retry logic.

### 3. Data Engineering

**Batch (Auto Loader).** Land C-MAPSS `.txt` files in an ADLS external location or UC volume; ingest incrementally:
```python
bronze = (spark.readStream.format("cloudFiles")
  .option("cloudFiles.format", "csv")
  .option("cloudFiles.schemaLocation", schema_path)
  .option("delimiter", " ")
  .load(landing_path))
```
Auto Loader gives exactly-once incremental ingestion with schema inference/evolution and `_rescued_data`. In Lakeflow pipelines you don't manage schema/checkpoint locations manually.

**REST API.** In a notebook task, call a public API (e.g., EIA energy data or Open-Meteo), write JSON to ADLS, ingest with Auto Loader — demonstrates API ingestion of "diverse sources."

**Streaming simulation (Event Hubs).** Because the Structured Streaming Event Hubs connector isn't in Databricks Runtime for Lakeflow, use the **Kafka-compatible endpoint** with the built-in Kafka connector, authenticating via SAS stored in a **secret scope**:
```python
kafka_opts = {
 "kafka.bootstrap.servers": f"{ns}.servicebus.windows.net:9093",
 "kafka.sasl.mechanism": "PLAIN",
 "kafka.security.protocol": "SASL_SSL",
 "kafka.sasl.jaas.config": f'...SharedAccessKey={dbutils.secrets.get(scope,"eh-key")}...',
 "subscribe": topic}
stream = spark.readStream.format("kafka").options(**kafka_opts).load()
```

**Processing at scale (PySpark) & feature engineering.** Compute RUL labels (max cycle − current cycle per unit), rolling-window sensor statistics, normalization per operating condition, lag features. Persist features to a **Feature Engineering in Unity Catalog** feature table (Delta table with a primary key), enabling training/serving consistency and lineage:
```python
from databricks.feature_engineering import FeatureEngineeringClient
fe = FeatureEngineeringClient()
fe.create_table(name="dev.ml.turbofan_features",
  primary_keys=["unit_number","time_in_cycles"], df=features_df)
```

### 4. ML Modeling

- **Task:** RUL regression (primary) on FD001–FD004; optionally incident-severity/nature classification on OSHA structured fields.
- **Training + tracking:** MLflow autologging; try gradient boosting (XGBoost/LightGBM) and a sequence model baseline.
- **Hyperparameter tuning:** Hyperopt (or Optuna) with distributed trials, logging each to MLflow.
- **Evaluation:** `mlflow.models.evaluate(model_uri, eval_data, targets="RUL", model_type="regressor")` yields RMSE, MAE, R²; add the custom NASA PHM asymmetric score as a custom metric.
- **Registry with aliases (Unity Catalog):**
```python
import mlflow; from mlflow import MlflowClient
mlflow.set_registry_uri("databricks-uc")
mlflow.register_model(model_uri, "prod.ml.turbofan_rul")
MlflowClient().set_registered_model_alias("prod.ml.turbofan_rul","champion",1)
model = mlflow.pyfunc.load_model("models:/prod.ml.turbofan_rul@champion")
```
Aliases (champion/challenger) replace legacy stages; production code references `@champion`, never a fixed version — promotion is one alias swap. UC supports up to 10 aliases per model.

### 5. Deployment

- **Real-time:** Mosaic AI Model Serving endpoint serving the champion model; enable **inference tables** to log requests/responses to a UC Delta table (payloads >1 MiB not logged; data lands <1 hour after query).
- **Batch inference:** Scheduled Lakeflow Job loads `@champion` and scores the fleet nightly into a Gold predictions table feeding the dashboard.
- **Champion/challenger (A/B):** Deploy challenger to the same endpoint with **traffic splitting** via Mosaic AI Gateway; compare on inference-table metrics before promoting via alias swap.

### 6. Monitoring & Automated Retraining

- **Lakehouse Monitoring** attached to the inference table using the **Inference profile** (needs prediction, label, model-version, timestamp columns). It generates a **profile metrics table** and a **drift metrics table** (drift types BASELINE and CONSECUTIVE; metrics include chi-squared, KS test, JS distance, TV distance, L-infinity) plus an auto dashboard.
- **Alerting:** Databricks SQL alerts on drift/performance thresholds (e.g., JS distance above threshold, or RMSE degradation once ground-truth labels arrive) → email/webhook.
- **Automated retraining:** A Lakeflow Job triggered on schedule or by a drift-alert webhook re-runs feature build → training → evaluation → conditional promotion (only if challenger beats champion on holdout), then reassigns the champion alias. A separate workflow updates the monitor when ground-truth labels arrive.

### 7. Generative AI Solution (RAG over safety documents)

- **Corpus:** OSHA SIR `Final Narrative` narratives (+ optionally scraped OSHA regulation text / 29 CFR 1904). Land in a Delta table, chunk into passages.
- **Vector Search:** Create a Mosaic AI Vector Search endpoint + a **Delta Sync index** with Databricks-managed embeddings (e.g., `databricks-gte-large-en`), auto-syncing from the Delta table.
- **Agent Framework RAG chain:** Retrieve context from Vector Search, assemble prompt, call a **Foundation Model API** (pay-per-token, e.g., `databricks-meta-llama-3-3-70b-instruct`), return grounded answer. Log with MLflow, register in UC, deploy to Model Serving — which auto-generates a **Review App** for stakeholder feedback.
- **Evaluation & tracing:** Mosaic AI Agent Evaluation / `mlflow.genai.evaluate()` with scorers (RelevanceToQuery, Safety, correctness, groundedness); MLflow Tracing records each retrieval/LLM step.
- **Guardrails:** Mosaic AI Gateway AI Guardrails (PII filtering, safety) at endpoint level.
- **Structured extraction use case:** Prompt the LLM to extract structured fields (event type, body part, cause) from raw narratives, comparing against OIICS-coded ground truth.

### 8. MLOps / CI-CD

- **Databricks Asset Bundles (DAB):** `databricks.yml` declares resources (jobs, pipelines, models, endpoints, dashboards) and **targets** (dev/staging/prod). `src/` holds Python modules (unit-tested with pytest), `resources/` holds YAML job/pipeline defs, notebooks call thin wrappers.
- **Environment isolation:** dev/staging/prod as separate **UC catalogs** with hard ACL boundaries; `mode: development` for dev, `mode: production` for staging/prod; `run_as` service principals in staging/prod.
- **Git + GitHub Actions:** PR → lint + unit test + `databricks bundle validate`; merge to main → deploy to staging + integration tests; tag/approval → deploy to prod. Authenticate via **OIDC** (no long-lived PATs).
```bash
databricks bundle validate
databricks bundle deploy --target staging
databricks bundle deploy --target prod
```

### 9. Scalability, Reliability, Security

- **Compute:** serverless where possible; cluster policies capping node types/autoscaling; **Photon** for SQL/ETL; single-node small clusters for cost control in a portfolio.
- **Governance/security:** Unity Catalog fine-grained access controls, row/column masking; **service principals** for jobs; **secret scopes / Azure Key Vault** for Event Hubs SAS and API keys; audit logs; PII handling (SIR contains employer names/addresses — mask or drop; use guardrails to prevent PII leakage in RAG answers).
- **Private networking:** VNet injection + Private Link for the workspace and ADLS; storage firewall with "Allow Azure trusted services."
- **Cost management:** auto-termination, spot/serverless, pay-per-token FM APIs for GenAI, DBU monitoring via system tables.

### 10. Dashboards / Advanced Analytics

- **AI/BI Dashboard:** fleet RUL distribution, units approaching failure, maintenance schedule, incident trends by NAICS/state/body part, drift/quality tiles.
- **Genie space:** business users ask NL questions ("Which units will fail in the next 20 cycles?", "Show amputation incidents in food manufacturing"); grounded in UC, governed by ACLs; publish with the dashboard to add an "Ask Genie" button.

### 11. Step-by-Step Implementation Guide

**Azure prerequisites:**
1. Azure subscription. New users get an Azure free account with **$200 credit to spend in the first 30 days**, plus free monthly amounts of popular services (per Microsoft's Azure Free Account FAQ); after that it becomes pay-as-you-go. Databricks compute is billed as DBUs on top of Azure VM/storage.
2. Resource group; **ADLS Gen2** storage account (hierarchical namespace) with containers: `landing`, `bronze`, `silver`, `gold`, `metastore`.
3. Azure Databricks workspace — select **Trial (Premium – 14-Days Free DBUs)** tier for free Premium DBUs (you still pay Azure VM/storage).
4. **Access Connector for Azure Databricks** (managed identity); grant it **Storage Blob Data Contributor** on the storage account.
5. **Unity Catalog metastore** (one per region) in the account console, pointing to the `metastore` container via the access connector resource ID; assign the workspace.
6. Create catalogs `dev`, `staging`, `prod`, each with schemas (`bronze`, `silver`, `gold`, `ml`, `genai`); external locations for ADLS containers.
7. Azure Event Hubs namespace + hub for streaming; store SAS key in a Databricks secret scope.

**Notebook-by-notebook structure:**
- `00_setup` — create catalogs/schemas/volumes/external locations, grants.
- `01_ingest_batch_autoloader` — C-MAPSS files → Bronze.
- `02_ingest_api` — REST pull → Bronze.
- `03_ingest_stream_eventhubs` — Kafka endpoint → Bronze.
- `04_lakeflow_pipeline` (DLP) — Bronze→Silver→Gold with expectations.
- `05_feature_engineering` — RUL labels + features → UC feature table.
- `06_train_tune_mlflow` — training, Hyperopt, `mlflow.evaluate`, register + alias.
- `07_serve_and_batch_infer` — serving endpoint + batch scoring.
- `08_lakehouse_monitoring` — create inference monitor + SQL alerts.
- `09_retraining_job` — conditional retrain/promote.
- `10_rag_vectorsearch` — chunk narratives, build index.
- `11_agent_framework_rag` — build/log/deploy agent + Review App + eval.
- `12_llm_structured_extraction` — extract OIICS fields.
- `13_dashboard_genie` — AI/BI dashboard + Genie space.
- `resources/*.yml`, `databricks.yml`, `.github/workflows/*.yml` — DAB + CI/CD.

**Repo folder structure:**
```
sentinelops/
  databricks.yml
  resources/ (jobs.yml, pipelines.yml, serving.yml, monitoring.yml)
  src/ (ingestion/, features/, training/, genai/, common/)
  notebooks/ (00_setup ... 13_dashboard_genie)
  tests/ (unit/, data/)
  .github/workflows/ (ci.yml, cd.yml)
```

**Job orchestration.** One Lakeflow Job DAG: ingest → DLP pipeline → feature build → train/evaluate → conditional promote → batch infer → refresh monitor → refresh dashboard. Separate scheduled job for the RAG index sync and agent eval.

**Estimated cost & free-credit strategy.** Use the Azure $200 free credit + Databricks 14-day Premium DBU trial; run single-node/small serverless clusters, auto-terminate at 10–20 min idle, use pay-per-token FM APIs (avoid provisioned throughput). Note a Microsoft Q&A user reported ~$50 of Azure VM/storage cost over 10 days even in the free-DBU trial — so terminate clusters aggressively and delete Event Hubs/Vector Search endpoints when idle. Realistic out-of-pocket if careful: a few tens of dollars. As an alternative for non-networking pieces, **Databricks Free Edition** offers ongoing no-cost serverless with quotas.

**Approximate time to complete.** ~40–60 hours: setup 4–6h; data engineering 10–12h; ML 8–10h; monitoring 4–6h; GenAI 8–10h; CI/CD 4–6h; dashboards/polish 4–6h.

### 12. Resume / Interview Presentation

**Traceability matrix (JD bullet → project component):**
- *Courageous safety leader / safety & environmental rules* → OSHA safety RAG assistant + incident analytics dashboard; PII guardrails; framing project around HSE outcomes.
- *Design/develop/deploy ML, advanced analytics, GenAI in production* → RUL model on Model Serving + RAG agent endpoint + AI/BI analytics.
- *Experiment tracking, versioning, evaluation, monitoring, drift, retraining* → MLflow + UC registry aliases + `mlflow.evaluate` + Lakehouse Monitoring + SQL alerts + automated retraining job.
- *Cross-functional collaboration → data-driven solutions* → stakeholder/KPI narrative + Genie self-service.
- *Large-scale data from diverse sources, modern data engineering* → Auto Loader batch + REST API + Event Hubs streaming + Lakeflow medallion.
- *Scalability, reliability, security of pipelines & models* → cluster policies, serverless, Photon, UC access control, service principals, Key Vault, Private Link, DAB CI/CD.

**Talking points:** why aliases beat stages; how expectations enforce data quality; how drift metrics trigger retraining; RAG groundedness/guardrails; cost trade-offs of pay-per-token vs provisioned throughput.

**Demo script (10 min):** (1) show UC lineage from Bronze to model; (2) trigger Lakeflow pipeline, show expectations; (3) open MLflow experiment + champion/challenger; (4) hit the serving endpoint live; (5) show Lakehouse Monitoring drift dashboard + a fired alert; (6) ask the RAG agent a safety question in the Review App, show the trace; (7) ask Genie a business question.

## Recommendations

1. **Start now with the Kaggle C-MAPSS mirror (CC0)** for the ML model — simplest license and confirmed working; cite Zenodo (record 15346912, CC-BY-4.0) / NASA S3 for provenance. Begin with **FD001 (100 train / 100 test, single condition/fault)** to get an end-to-end skeleton, then scale to FD004 (248/249, 6 conditions, 2 faults).
2. **Build the skeleton thin, then deepen.** Get one path working end-to-end (batch ingest → DLP → train → register → serve → monitor) before adding streaming, RAG, and CI/CD. This guarantees a demoable artifact early.
3. **Add the OSHA RAG component second** — it is the differentiator that hits the "safety leader" bullet most other candidates will miss. The `Final Narrative` field across all 105,991 SIR records is your grounding corpus.
4. **Control cost aggressively:** single-node clusters, auto-terminate, pay-per-token FM APIs, delete Vector Search/Event Hubs when idle. Benchmark: if daily Azure cost exceeds ~$5–10, downsize compute or switch to Databricks Free Edition for non-networking pieces.
5. **Escalate scope only if targeting senior roles:** swap C-MAPSS for N-CMAPSS (HDF5, ~15.9 GB) to prove large-scale handling, and add provisioned-throughput serving discussion.
6. **Thresholds that change the plan:** if the OSHA full CSV download is unavailable, fall back to filtered dashboard exports or the legacy zip (`osha.gov/sites/default/files/severe_injury.zip`); if Vector Search quota is unavailable on trial, demonstrate RAG with a smaller in-notebook FAISS prototype but document the Vector Search production design.

## Caveats
- **Dataset availability shifts.** NASA's PCoE repository and data.gov have moved links repeatedly; the specific `data.nasa.gov` ff5v-kuh6 URL is dead (404). Verify the Kaggle/Zenodo/S3 links at build time. Kaggle mirror is CC0; the official Zenodo NASA copy is CC-BY-4.0 (attribution required) — license diverges by source.
- **N-CMAPSS** is ~15.9 GB (Kaggle mirror, "U.S. Government Works") and one sub-dataset (DS008d) is reportedly corrupted; treat as optional advanced scope.
- **OSHA SIR** currently holds 105,991 records through 11/30/2025 (federal-jurisdiction only — excludes state-plan states and fatalities), contains PII (employer names/addresses), and geocodes are approximate/third-party-derived — handle accordingly.
- **Feature currency.** Databricks renames features frequently; DLT→Lakeflow, Workflows→Lakeflow Jobs, Databricks One→Genie One, and the feature-store package rename are all recent. Some agent inference-table logging APIs (`payload_request_logs`/`payload_assessment_logs`) were deprecated December 4, 2025 in favor of MLflow 3 Traces — use current MLflow 3 tracing/monitoring.
- **Cost is real even in trials.** Azure VM/storage charges apply during the free-DBU Databricks trial; a documented case incurred ~$50 in 10 days. Budget accordingly.
- Pricing figures (DBU rates, FM API token prices) change frequently; verify at the Azure/Databricks pricing pages before quoting in an interview.