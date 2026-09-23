# Operational ML: promotion, fleet scoring and monitoring

This milestone puts the Gold-trained model into a batch operating loop. There is
still no real-time serving endpoint (see "Serving" below).

```mermaid
flowchart LR
    T[cmapss_train] -->|@challenger| P[cmapss_promote]
    P -->|validation gate| C[@champion]
    G[Gold features, fleet split] --> S[score task]
    C --> S
    S --> L[gold.cmapss_predictions]
    E[Official endpoint labels] --> M[monitor task]
    L --> M
    M --> L
    M --> R[gold.cmapss_model_performance]
    M --> D[gold.cmapss_feature_drift]
```

## Promotion gate (`cmapss_promote`)

`jobs/promote.py` evaluates the `@challenger` version and never reads official
test labels. It blocks promotion unless all of these hold:

- The version was trained from Gold (`training_source=gold_medallion` tag), and
  its signature equals the Gold feature contract (`sentinelops.model.FEATURES`).
- The Gold training labels still match the digest the challenger's run logged.
  The gate then recomputes a constant-mean baseline on the challenger's own
  held-out validation engines. The challenger's validation RMSE must be at most
  **0.5×** that baseline; the ratio was fixed before the first evaluation.
- If a champion exists, both must share subset, validation engines and label
  digest, and the challenger must match or beat the champion's validation RMSE
  (tolerance 0). If they aren't comparable, the gate rejects rather than guessing.

On success it moves `@champion`, removes `@challenger`, and tags the version with
`promotion_decision` and `promotion_reasons`. A rejection only writes those tags.
Validation RMSE comes from the model-selection step (fit on 80% of engines).
It measures the training procedure on unseen engines, not the refit model.

## Fleet scoring (`cmapss_score`, task `score`)

In this simulation the NASA **test engines play the in-service fleet**: their
trajectories stop before failure, like engines still flying. `jobs/score_fleet.py`
resolves `@champion` once, loads that exact version, and scores Gold feature rows
not yet logged for that version. Gold features are the same Spark computation
the model was trained on, which matters because the model is sensitive to the
~1e-12 Spark/pandas differences (see STATUS.md).

`gold.cmapss_predictions` is the inference log. Its composite key is
`(dataset, subset, split, unit, cycle, model_version)`. Writes are an
insert-only MERGE on that key, so reruns add nothing, and a newly promoted
version scores the whole fleet once. Each row records model name/version,
`scored_at` and the job run ID. `actual_rul` and `label_observed_at` stay null
until ground truth arrives.

## Delayed labels and monitoring (task `monitor`)

An engine's ground truth arrives with its official endpoint label. The true RUL
at an earlier cycle is then `endpoint RUL + (last cycle − cycle)`, uncapped.
`jobs/monitor_fleet.py` merges these labels into the log only where they are
missing. It then appends snapshots, each stamped with `computed_at` and the job
run:

- `gold.cmapss_model_performance`: rows, RMSE, MAE, NASA score and bias
  (positive means RUL is overestimated, the unsafe direction). Reported per
  model version and segment: `all`, `endpoint` (last cycle, i.e. the official
  benchmark), `actual_le_125`, and true-RUL buckets. The model was trained with
  labels capped at 125, so it underestimates healthy engines by design (bucket
  `rul_126_plus`). `all` mixes that in, and its NASA score is not meaningful.
- `gold.cmapss_feature_drift`: for each model input, PSI of fleet vs training
  features. Fleet engines are observed earlier in life than run-to-failure
  training data, so raw `psi` mostly measures age. `psi_age_matched` reweights
  the training reference to the fleet's cycle distribution and drops training
  rows outside the fleet's age range.

Because the fleet here is the benchmark test set, these metrics are operational
evidence only. They must not choose or promote models; the gate above does that.
No alert thresholds or schedules are configured yet.

## Serving (deferred)

No Model Serving endpoint exists. The model needs 10 cycles of history per
engine, so a real-time contract must either:

- accept Gold-format feature rows computed by the Spark pipeline, or
- look features up by engine key from a synced online store.

It must not recompute features with pandas. An endpoint (and inference tables)
should be created only for a bounded demo and deleted afterwards, because
provisioned serving compute bills while it is up.

## Runbook

```powershell
. ./scripts/Use-SentinelOps.ps1
# After cmapss_train registers a new @challenger:
.tools/databricks/databricks.exe bundle run -t dev cmapss_promote
# Score new fleet rows with @champion, merge labels, snapshot metrics:
.tools/databricks/databricks.exe bundle run -t dev cmapss_score
```

All jobs are manual, STANDARD performance, one concurrent run, and zero retries.
`cmapss_promote` has a 600-second limit; `cmapss_score` has 1,200 seconds
(600 per task).
