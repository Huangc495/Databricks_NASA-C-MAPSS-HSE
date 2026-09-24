"""Structured-extraction evaluation: OSHA injury coding from narratives, scored against OSHA's codes.

For each model, ai_query with a JSON-schema responseFormat codes the held-out test narratives
(event, nature, body part, source). Raw responses are stored once in gold.osha_extractions,
keyed by (report_id, model, prompt_version), so paid calls never repeat; failed rows are left
for a rerun. A two-row preflight on dev reports fails fast if the endpoint rejects the request.
Every stored response is validated in code, then scored on harmonized truth
(sentinelops.extraction) against a majority-class baseline and a supervised TF-IDF + logistic
regression baseline trained on the train split. Results go to MLflow.
"""
import argparse
import json
import re
import sys
import time

parser = argparse.ArgumentParser()
parser.add_argument("--catalog", required=True)
parser.add_argument("--models", required=True, help="Comma-separated chat endpoints")
parser.add_argument("--reasoning-effort", required=True)
parser.add_argument("--max-tokens", type=int, required=True)
parser.add_argument("--partitions", type=int, required=True)
parser.add_argument("--job-run-id", required=True)
parser.add_argument("--source-root", required=True)
args = parser.parse_args()
# Serverless Python tasks execute via exec(), where __file__ is not defined.
sys.path.insert(0, args.source_root)

import mlflow
import pandas as pd
from pyspark.sql import SparkSession, functions as F

from sentinelops import extraction as x

models = args.models.split(",")
if not re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_]*", args.catalog) or not all(re.fullmatch(r"[a-z0-9-]+", m) for m in models):
    raise ValueError("Catalog must be a simple identifier and models serving endpoint names")
if not re.fullmatch(r"[0-9]+", args.job_run_id) or args.reasoning_effort not in ("low", "medium", "high"):
    raise ValueError("Job run ID must be numeric and reasoning effort low, medium or high")
schema_json = json.dumps(x.SCHEMA)
if "'" in schema_json:
    raise ValueError("The response schema must not contain single quotes (it is inlined in SQL)")

started = time.monotonic()
spark = SparkSession.builder.getOrCreate()
gold = f"{args.catalog}.gold"
columns = ["report_id", "event_month", "narrative", "nature_title"] + \
          [f"{field}_{kind}" for field in ("event", "body_part", "source") for kind in ("code", "title")]
frame = spark.table(f"{gold}.osha_documents").select(*columns).orderBy("report_id").toPandas()
truth, harmonized = x.truth(frame)
truth["split"] = frame.report_id.map(x.split).to_numpy()
narratives = dict(zip(frame.report_id, frame.narrative))
test_truth = truth[truth.split == "test"].reset_index(drop=True)

# Baselines: the most frequent training label, and a supervised model trained on train narratives.
train = truth[truth.split == "train"]
majority_labels = x.majority(train)
baselines = {"majority": pd.DataFrame({"report_id": test_truth.report_id, **majority_labels})}
supervised = pd.DataFrame({"report_id": test_truth.report_id})
fit_seconds = {}
for field in x.FIELDS:
    rows = train[train[field].notna()]
    fit_started = time.monotonic()
    model = x.fit_supervised([narratives[r] for r in rows.report_id], rows[field])
    fit_seconds[field] = round(time.monotonic() - fit_started, 1)
    supervised[field] = model.predict([narratives[r] for r in test_truth.report_id])
baselines["supervised_tfidf_logreg"] = supervised

target = f"{gold}.osha_extractions"
staging = f"{gold}.osha_extractions_staging"
spark.sql(f"""
    CREATE TABLE IF NOT EXISTS {target} (
      report_id BIGINT NOT NULL, model STRING NOT NULL, prompt_version STRING NOT NULL,
      response STRING NOT NULL, extracted_at TIMESTAMP NOT NULL, extraction_job_run STRING NOT NULL,
      CONSTRAINT osha_extractions_pk PRIMARY KEY (report_id, model, prompt_version))
    COMMENT 'Raw JSON injury coding of OSHA narratives by chat models (validated when read); test split only.'
""")


def ai_query_sql(model: str, view: str) -> str:
    return f"""
        SELECT report_id, response.result AS response, response.errorMessage AS error
        FROM (SELECT /*+ REPARTITION({args.partitions}) */ report_id,
                     ai_query('{model}', request, responseFormat => '{schema_json}',
                              modelParameters => named_struct('max_tokens', {args.max_tokens}, 'temperature', 0.0,
                                                              'reasoning_effort', '{args.reasoning_effort}'),
                              failOnError => false) AS response
              FROM {view})"""


def requests_frame(report_ids):
    return spark.createDataFrame([(int(r), x.request(narratives[r])) for r in report_ids], "report_id BIGINT, request STRING")


calls = {}
for model in models:
    # Preflight on two dev reports: an unsupported parameter fails here, not after a full batch.
    requests_frame(truth[truth.split == "dev"].report_id[:2]).createOrReplaceTempView("preflight")
    preflight = spark.sql(ai_query_sql(model, "preflight")).collect()
    if all(row.error for row in preflight):
        raise RuntimeError(f"{model} preflight failed: {preflight[0].error[:300]}")
    if all(x.parse(row.response)[1] for row in preflight):  # e.g. an unexpected response shape
        raise RuntimeError(f"{model} preflight returned no valid extraction: {str(preflight[0].response)[:300]}")
    stored = spark.table(target).filter((F.col("model") == model) & (F.col("prompt_version") == x.PROMPT_VERSION))
    done = {row.report_id for row in stored.select("report_id").collect()}
    pending = [r for r in test_truth.report_id if int(r) not in done]
    calls[model] = {"pending": len(pending), "failed": 0, "seconds": 0.0}
    if pending:
        batch_started = time.monotonic()
        requests_frame(pending).createOrReplaceTempView("pending_requests")
        # Written once, so the paid calls are never re-executed by a second read.
        spark.sql(f"CREATE OR REPLACE TABLE {staging} AS {ai_query_sql(model, 'pending_requests')}")
        calls[model]["failed"] = spark.table(staging).filter("response IS NULL").count()
        calls[model]["sample_error"] = spark.table(staging).agg(F.max("error")).first()[0]
        spark.sql(f"""
            MERGE INTO {target} t
            USING (SELECT report_id, response FROM {staging} WHERE response IS NOT NULL) s
            ON t.report_id = s.report_id AND t.model = '{model}' AND t.prompt_version = '{x.PROMPT_VERSION}'
            WHEN NOT MATCHED THEN INSERT (report_id, model, prompt_version, response, extracted_at, extraction_job_run)
              VALUES (s.report_id, '{model}', '{x.PROMPT_VERSION}', s.response, current_timestamp(), '{args.job_run_id}')
        """)
        spark.sql(f"DROP TABLE {staging}")
        calls[model]["seconds"] = round(time.monotonic() - batch_started, 1)

# Validate every stored response in code; missing or invalid ones count as wrong.
predictions, invalid = {}, {}
for model in models:
    stored = spark.table(target).filter((F.col("model") == model) & (F.col("prompt_version") == x.PROMPT_VERSION)) \
        .select("report_id", "response").toPandas()
    responses = dict(zip(stored.report_id, stored.response))
    rows, reasons = [], []
    for report_id in test_truth.report_id:
        parsed, error = x.parse(responses.get(report_id))
        if error:
            reasons.append(re.split(r"[:=]", error)[0])  # e.g. "empty response", "invalid JSON", "source"
        rows.append({"report_id": report_id, **{f: (parsed or {}).get(f) for f in x.FIELDS}})
    predictions[model] = pd.DataFrame(rows)
    invalid[model] = pd.Series(reasons, dtype=str).value_counts().to_dict()

methods = {**baselines, **predictions}
scores = {name: x.score(test_truth, frame_) for name, frame_ in methods.items()}
comparisons = {model: {field: x.paired_bootstrap(x.correct(test_truth, predictions[model], field),
                                                 x.correct(test_truth, supervised, field))
                       for field in x.FIELDS} for model in models}
if len(models) > 1:
    comparisons[f"{models[0]}_vs_{models[1]}"] = {
        field: x.paired_bootstrap(x.correct(test_truth, predictions[models[0]], field),
                                  x.correct(test_truth, predictions[models[1]], field)) for field in x.FIELDS}
confusion = {model: {field: pd.crosstab(test_truth[field], predictions[model][field].fillna("invalid"))
                     .to_dict() for field in x.FIELDS} for model in models}

user = spark.sql("SELECT current_user()").first()[0]
mlflow.set_experiment(f"/Users/{user}/sentinelops-safety-rag")
with mlflow.start_run(run_name=f"extraction-eval-{x.VERSION}") as run:
    mlflow.log_params({"eval_version": x.VERSION, "prompt_version": x.PROMPT_VERSION, "models": args.models,
                       "reasoning_effort": args.reasoning_effort, "max_tokens": args.max_tokens, "temperature": 0.0,
                       "test_reports": len(test_truth), "train_reports": len(train), "job_run_id": args.job_run_id})
    mlflow.log_metrics({f"{re.sub(r'[^a-z0-9_]', '_', name)}_{field}_{metric}": value
                        for name, fields in scores.items() for field, values in fields.items()
                        for metric, value in values.items() if isinstance(value, float)})
    mlflow.log_text(x.PROMPT, "prompt.txt")
    mlflow.log_dict(x.SCHEMA, "response_schema.json")
    report = {"mlflow_run_id": run.info.run_id, "prompt_version": x.PROMPT_VERSION, "documents": len(frame),
              "splits": truth.split.value_counts().to_dict(), "harmonized_truth_changes": harmonized,
              "scoreable_test": {f: int(test_truth[f].notna().sum()) for f in x.FIELDS},
              "test_by_era": test_truth.era.value_counts().to_dict(), "majority_labels": majority_labels,
              "scores": scores, "comparisons_vs_supervised": comparisons, "invalid_outputs": invalid, "calls": calls,
              "supervised_fit_seconds": fit_seconds, "seconds": round(time.monotonic() - started, 1)}
    mlflow.log_dict(report, "extraction_report.json")
    mlflow.log_dict(confusion, "confusion_matrices.json")
print(json.dumps(report, default=str))
