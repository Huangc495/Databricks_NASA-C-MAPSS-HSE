# Auto Loader and Lakeflow ingestion

This milestone reuses `sentinelops_dev`, its managed ADLS storage, and the existing
`sentinelops_dev.sentinelops_dev.landing` volume. No new Azure infrastructure or
external-location grants are needed. Bronze, Silver and Gold have separate UC
schemas. The original bootstrap tables, job and model version 2 remain available.
The `cmapss_train` job trains from the Gold tables; see "Train from Gold" below.

## Data contract

`python -m sentinelops.landing` rechecks the archive MD5, validates both FD001
files, copies their original bytes, and creates explicit engine-keyed JSON from
the official RUL file's ordered lines. A SHA256 manifest records landed content.
The versioned landing prefix is immutable: never overwrite a consumed file or
reset a pipeline checkpoint to introduce a correction.

- Bronze `cmapss_lines`: Auto Loader TEXT, original line and file provenance.
  TEXT has a fixed schema; malformed numeric content stays in the original line
  rather than a synthetic `_rescued_data` column.
- Bronze `cmapss_labels`: Auto Loader JSON, explicit schema and rescued-data field.
- Silver `cmapss_quarantine`: invalid width, filename, keys or numeric values.
- Silver `cmapss_conflicts`: keys with more than one distinct numeric payload.
  The original payloads and source paths remain in Bronze.
- Silver `cmapss_observations`: valid rows, equal-payload duplicates collapsed,
  conflicting keys excluded. Expectations record quality outcomes.
- Silver `cmapss_endpoint_labels`: fail on rescued, invalid or duplicate labels.
- Gold `cmapss_features`: current sensors, trailing ten observations and five-row
  differences, partitioned by dataset/subset/split/unit. Composite UC primary key:
  `(dataset, subset, split, unit, cycle)`. Keys are informational; the graph and
  integration assertions enforce uniqueness.
- Gold `cmapss_training_labels`: capped remaining life from completed training
  trajectories only. This benchmark input is sealed run-to-failure data; do not
  land unfinished fleet trajectories as `train`.
- Gold `cmapss_test_endpoints`: last observed test cycle joined to the official,
  uncapped endpoint label. No test labels enter features.

Bronze is checkpointed incremental ingestion. Silver and Gold are materialized
views with batch semantics: Lakeflow chooses incremental refresh or recomputation.
This permits global conflict detection and correct historical windows without an
unbounded streaming deduplication state store. For this small benchmark,
recomputation is acceptable; larger fleet workloads need separate design/testing.
Late earlier cycles can legitimately change later features on refresh. Windows
are past-only at each cycle, not a guarantee that historical revisions are frozen.

## Run and verify

```powershell
. ./scripts/Use-SentinelOps.ps1
.venv/Scripts/python.exe -m sentinelops.landing
# First upload only. Do not use --overwrite for consumed inputs.
.tools/databricks/databricks.exe fs cp data/landing/v1 dbfs:/Volumes/sentinelops_dev/sentinelops_dev/landing/cmapss_ingest/v1 --recursive
.tools/databricks/databricks.exe bundle validate --strict -t dev
.tools/databricks/databricks.exe bundle deploy -t dev
# Inspect current costs/compute and allow for billing delay before any run.
.tools/databricks/databricks.exe bundle run -t dev cmapss_ingest --no-wait
# After ingestion succeeds, run independent parity/billing checks:
.tools/databricks/databricks.exe bundle run -t dev cmapss_verify --no-wait
```

The separate integration job verifies every table count, all feature values against the
bootstrap implementation, official test labels, training labels, and the declared
primary key. Evidence is saved to `cmapss_ingest/v1/verification.json` in the volume.
Do not run `--full-refresh` for normal ingestion; it resets ingestion state.

## Train from Gold

```powershell
# After a completed cmapss_ingest update; never concurrently with one.
.tools/databricks/databricks.exe bundle run -t dev cmapss_train
```

`jobs/train_medallion.py` reads one subset (`FD001`) from `gold.cmapss_features`,
`gold.cmapss_training_labels` and `gold.cmapss_test_endpoints`. It fails unless
every training feature key has exactly one label (and vice versa), labels respect
the 125-cycle cap, and every test engine has exactly one official label. Rows are
restored to bootstrap `(unit, cycle)` order, so engine-disjoint selection is
reproducible. The run logs content digests of the exact rows used, MLflow dataset
inputs naming the Gold tables, upstream quarantine/conflict counts, metrics and
predictions, then registers a tagged UC version and moves `challenger` to it.
Quarantined and conflicting rows never reach training.

## Cost controls

The ingestion job is manual, standard performance, one concurrent run, no
application/flow/update retries, and a 900-second overall timeout. The pipeline
is triggered and has development mode explicitly disabled at the bundle preset
level so that it does not retain development compute. There is no recurring
schedule. These controls reduce exposure but are not a $10 hard cutoff: serverless
scaling and delayed billing prevent a strict dollar guarantee. Review posted Azure
costs and available Databricks usage before adding runs; stop discretionary tests
if headroom cannot be established. The starter warehouse stays stopped.
Verification has its own 600-second limit and does not refresh the pipeline.
The first combined run completed ingestion but timed out during the second
compute startup; separating the jobs avoids repeating successful ingestion.
STANDARD mode can wait ~7 minutes for resources; ingestion runs have taken
11–12 minutes of their 15-minute limit. If one times out while waiting,
rerun it before raising the limit.

The optional `scripts/prepare_quality_probe.py` prepares a nine-line file outside
the normal upload directory: two repeated canonical observations, five malformed
rows, and two conflicting payloads for a synthetic unit 999. Upload it only for
an intentional quality test. The verifier recognizes this file and requires
exactly five quarantined rows, one conflicting key, and unchanged canonical
counts/features. This tests incremental ingestion and duplicate resistance;
a subsequent no-input run separately tests checkpoint replay behavior.

The incremental test ran on September 23, 2026 using these steps. Don't upload
the probe again:

```powershell
.venv/Scripts/python.exe scripts/prepare_quality_probe.py
.tools/databricks/databricks.exe fs cp data/quality_probe/train_FD001_quality.txt dbfs:/Volumes/sentinelops_dev/sentinelops_dev/landing/cmapss_ingest/v1/trajectories/train_FD001_quality.txt
.tools/databricks/databricks.exe bundle run -t dev cmapss_ingest
.tools/databricks/databricks.exe bundle run -t dev cmapss_verify
# Recheck costs before a further no-input ingestion + verification run.
```

The probe has now been uploaded and has passed. The volume therefore
permanently contains it, and the verifier expects its counts on every later
run. Bronze appended only the 9 probe lines, and the expected 5 quarantined
rows and 1 conflict appeared with canonical counts unchanged. A later no-input
update appended nothing and planned every materialized view as NO_OP.
Summarize any update's flow metrics and refresh techniques with
`scripts/pipeline_update_evidence.py`. The CLI's `list-pipeline-events`
leaves out event details, so use the REST events API as that script's
docstring shows. See STATUS.md for run IDs and evidence files.

## Official references checked September 23, 2026

- [Auto Loader schema behavior](https://learn.microsoft.com/en-us/azure/databricks/ingestion/cloud-object-storage/auto-loader/schema)
- [Lakeflow Python API and declaration restrictions](https://learn.microsoft.com/en-us/azure/databricks/ldp/developer/python-ref)
- [Serverless pipeline modes](https://learn.microsoft.com/en-us/azure/databricks/ldp/serverless)
- [Unity Catalog feature table keys](https://learn.microsoft.com/en-us/azure/databricks/machine-learning/feature-store/uc/feature-tables-uc)
