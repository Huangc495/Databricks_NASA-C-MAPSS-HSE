"""Retrieval evaluation for the OSHA assistant, logged to MLflow.

Exact dense search at 1,024 and 256 (Matryoshka-truncated) dimensions against a
TF-IDF baseline, scored with OSHA-code relevance rules (sentinelops.retrieval_eval).
Questions are embedded once with ai_query using the query instruction. Off-topic
questions measure how well on- and off-topic scores separate, to inform abstention.
"""
import argparse
import json
import re
import sys
import time

parser = argparse.ArgumentParser()
parser.add_argument("--catalog", required=True)
parser.add_argument("--endpoint", required=True)
parser.add_argument("--k", type=int, required=True)
parser.add_argument("--job-run-id", required=True)
parser.add_argument("--source-root", required=True)
args = parser.parse_args()
# Serverless Python tasks execute via exec(), where __file__ is not defined.
sys.path.insert(0, args.source_root)

import mlflow
import numpy as np
import pandas as pd
from pyspark.sql import SparkSession, functions as F

from sentinelops.embeddings import DIMENSIONS, format_query, unit_vectors
from sentinelops.retrieval import ExactIndex, KeywordIndex, truncate
from sentinelops.retrieval_eval import OFF_TOPIC, QUESTIONS, VERSION, fingerprint, relevance, score, summarize

if not re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_]*", args.catalog) or not re.fullmatch(r"[a-z0-9-]+", args.endpoint):
    raise ValueError("Catalog must be a simple identifier and endpoint a serving endpoint name")

spark = SparkSession.builder.getOrCreate()
gold = f"{args.catalog}.gold"
titles = ["nature_title", "body_part_title", "event_title", "source_title", "secondary_source_title"]
documents = spark.table(f"{gold}.osha_documents").select("report_id", "document", "document_sha256", *titles)
embeddings = spark.table(f"{gold}.osha_embeddings").filter(F.col("model") == args.endpoint) \
    .select("report_id", "document_sha256", "embedding")
# Only embeddings of the current document text count; a stale vector would silently skew results.
frame = documents.join(embeddings, ["report_id", "document_sha256"]).orderBy("report_id").toPandas()
if len(frame) != documents.count():
    raise ValueError(f"{documents.count() - len(frame)} documents lack a current embedding; run osha_embed first")
matrix = unit_vectors(np.stack(frame.pop("embedding").to_numpy()), DIMENSIONS)
ids = frame.report_id.to_numpy()

asked = QUESTIONS + OFF_TOPIC
query_frame = spark.createDataFrame([(q["id"], format_query(q["question"])) for q in asked], "id STRING, text STRING")
vectors = {row.id: row.e for row in query_frame.selectExpr("id", f"ai_query('{args.endpoint}', text) AS e").collect()}
queries = unit_vectors([vectors[q["id"]] for q in asked], DIMENSIONS)

truth, on = relevance(frame), len(QUESTIONS)
indexes = {"dense_1024": (ExactIndex(ids, matrix), queries),
           "dense_256": (ExactIndex(ids, truncate(matrix, 256)), truncate(queries, 256))}
rows, off_topic, latency_ms, rankings = [], {}, {}, {}
for name, (index, query_vectors) in indexes.items():
    started = time.perf_counter()
    found, scores = index.search(query_vectors, args.k)
    latency_ms[name] = (time.perf_counter() - started) * 1000 / len(query_vectors)
    rows += score(name, found[:on], scores[:on], truth, k=args.k)
    off_topic[name] = {q["id"]: round(float(s[0]), 4) for q, s in zip(OFF_TOPIC, scores[on:])}
    rankings[name] = found[:on]
started = time.perf_counter()
keyword = KeywordIndex(ids, frame.document.tolist())
fit_seconds = time.perf_counter() - started
started = time.perf_counter()
found, scores = keyword.search([q["question"] for q in QUESTIONS], args.k)
latency_ms["tfidf"] = (time.perf_counter() - started) * 1000 / on
rows += score("tfidf", found, scores, truth, k=args.k)

rows = pd.DataFrame(rows)
summary = summarize(rows)
overlap = np.mean([len(set(a) & set(b)) / args.k for a, b in zip(rankings["dense_1024"], rankings["dense_256"])])
separation = {}
for name in indexes:
    on_topic = rows[rows.method == name].top1_score
    separation[name] = {"min_on_topic_top1": round(float(on_topic.min()), 4),
                        "max_off_topic_top1": max(off_topic[name].values()),
                        "separated": bool(on_topic.min() > max(off_topic[name].values()))}

user = spark.sql("SELECT current_user()").first()[0]
mlflow.set_experiment(f"/Users/{user}/sentinelops-safety-rag")
with mlflow.start_run(run_name=f"retrieval-eval-{VERSION}") as run:
    mlflow.log_params({"eval_version": VERSION, "eval_fingerprint": fingerprint(), "embedding_model": args.endpoint,
                       "documents": len(frame), "k": args.k, "questions": on, "off_topic_questions": len(OFF_TOPIC),
                       "job_run_id": args.job_run_id})
    mlflow.log_metrics({f"{method}_{metric}": float(value) for method, values in summary.iterrows()
                        for metric, value in values.items()})
    mlflow.log_metrics({f"{name}_latency_ms": value for name, value in latency_ms.items()})
    mlflow.log_metrics({"top10_overlap_1024_vs_256": overlap, "tfidf_fit_seconds": fit_seconds,
                        "dense_1024_index_mb": indexes["dense_1024"][0].megabytes,
                        "dense_256_index_mb": indexes["dense_256"][0].megabytes})
    mlflow.log_table(rows.assign(top_ids=rows.top_ids.map(json.dumps)), "per_question.json")
    report = {"mlflow_run_id": run.info.run_id, "documents": len(frame), "eval_fingerprint": fingerprint(),
              "summary": summary.to_dict("index"), "latency_ms_per_query": {k: round(v, 2) for k, v in latency_ms.items()},
              "index_mb": {n: round(i.megabytes, 1) for n, (i, _) in indexes.items()},
              "top10_overlap_1024_vs_256": round(float(overlap), 4), "off_topic_top1": off_topic, "separation": separation,
              "per_question_p_at_10": rows.pivot(index="question_id", columns="method", values="p_at_10").round(2).to_dict("index")}
    mlflow.log_dict(report, "retrieval_report.json")
print(json.dumps(report, default=str))
