"""Incrementally embed Gold OSHA documents into gold.osha_embeddings with ai_query.

Only documents whose (report_id, document_sha256) has no embedding for this model are
sent. Results are written once to a staging table, so the paid model calls are never
re-executed by a second read. Only valid unit-length vectors are merged; failures are
counted and left for the next run. Embeddings of reports no longer in Gold are deleted.

Measured 2026-09-24: ai_query embedded ~470 documents/s here, while direct REST calls
to the same pay-per-token endpoint are throttled to ~24 inputs/s (see docs/SAFETY_RAG.md).
"""
import argparse
import json
import re
import sys
import time

parser = argparse.ArgumentParser()
parser.add_argument("--catalog", required=True)
parser.add_argument("--endpoint", required=True)
parser.add_argument("--partitions", type=int, required=True)
parser.add_argument("--job-run-id", required=True)
parser.add_argument("--source-root", required=True)
args = parser.parse_args()
# Serverless Python tasks execute via exec(), where __file__ is not defined.
sys.path.insert(0, args.source_root)

from pyspark.sql import SparkSession, functions as F

from sentinelops.embeddings import DIMENSIONS

if not re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_]*", args.catalog) or not re.fullmatch(r"[a-z0-9-]+", args.endpoint):
    raise ValueError("Catalog must be a simple identifier and endpoint a serving endpoint name")
if not re.fullmatch(r"[0-9]+", args.job_run_id):
    raise ValueError("Job run ID must be numeric")

started = time.monotonic()
spark = SparkSession.builder.getOrCreate()
documents = f"{args.catalog}.gold.osha_documents"
target, staging = f"{args.catalog}.gold.osha_embeddings", f"{args.catalog}.gold.osha_embeddings_staging"
spark.sql(f"""
    CREATE TABLE IF NOT EXISTS {target} (
      report_id BIGINT NOT NULL, document_sha256 STRING NOT NULL, model STRING NOT NULL,
      dimensions INT NOT NULL, embedding ARRAY<FLOAT> NOT NULL,
      embedded_at TIMESTAMP NOT NULL, embedding_job_run STRING NOT NULL,
      CONSTRAINT osha_embeddings_pk PRIMARY KEY (report_id, model))
    COMMENT 'Unit-length document embeddings for exact retrieval; one row per report and model.'
""")

current = spark.table(target).filter(F.col("model") == args.endpoint).select("report_id", "document_sha256")
spark.table(documents).select("report_id", "document_sha256", "document") \
    .join(current, ["report_id", "document_sha256"], "left_anti").createOrReplaceTempView("pending_documents")
spark.sql(f"""
    CREATE OR REPLACE TABLE {staging} AS
    SELECT report_id, document_sha256, length(document) AS characters, response.errorMessage AS error,
           CAST(response.result AS ARRAY<FLOAT>) AS embedding,
           abs(aggregate(response.result, 0D, (acc, x) -> acc + x * x) - 1) AS norm_error
    FROM (SELECT /*+ REPARTITION({args.partitions}) */ report_id, document_sha256, document,
                 ai_query('{args.endpoint}', document, failOnError => false) AS response
          FROM pending_documents)
""")
valid = f"error IS NULL AND size(embedding) = {DIMENSIONS} AND norm_error < 1e-3"
summary = spark.sql(f"""
    SELECT count(*) AS pending, count_if({valid}) AS embedded, count_if(NOT coalesce({valid}, false)) AS failed,
           coalesce(sum(characters), 0) AS characters, max(error) AS sample_error
    FROM {staging}
""").first().asDict()
merged = spark.sql(f"""
    MERGE INTO {target} t
    USING (SELECT report_id, document_sha256, embedding FROM {staging} WHERE {valid}) s
    ON t.report_id = s.report_id AND t.model = '{args.endpoint}'
    WHEN MATCHED THEN UPDATE SET document_sha256 = s.document_sha256, dimensions = {DIMENSIONS},
      embedding = s.embedding, embedded_at = current_timestamp(), embedding_job_run = '{args.job_run_id}'
    WHEN NOT MATCHED THEN INSERT (report_id, document_sha256, model, dimensions, embedding, embedded_at, embedding_job_run)
      VALUES (s.report_id, s.document_sha256, '{args.endpoint}', {DIMENSIONS}, s.embedding, current_timestamp(),
              '{args.job_run_id}')
""").first().asDict()
spark.sql(f"DROP TABLE {staging}")
removed = spark.sql(f"""
    DELETE FROM {target} WHERE model = '{args.endpoint}'
      AND report_id NOT IN (SELECT report_id FROM {documents})
""").first().asDict()

stored = spark.table(target).filter(F.col("model") == args.endpoint)
stale = stored.join(spark.table(documents).select("report_id", "document_sha256"), ["report_id", "document_sha256"], "left_anti")
report = {**summary, "merge": merged, "deleted": removed, "model": args.endpoint, "dimensions": DIMENSIONS,
          "documents": spark.table(documents).count(), "stored": stored.count(), "stale_or_orphaned": stale.count(),
          "wrong_size": stored.filter(F.size("embedding") != DIMENSIONS).count(),
          "by_run": {r.embedding_job_run: r["count"] for r in stored.groupBy("embedding_job_run").count().collect()},
          "seconds": round(time.monotonic() - started, 1)}
print(json.dumps(report, default=str))
