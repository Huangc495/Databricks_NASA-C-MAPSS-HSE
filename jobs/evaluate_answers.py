"""Grounded-answer evaluation for the OSHA assistant, traced and logged to MLflow.

Loads the 256-dimension exact index once (current vectors only) and embeds every question
once with ai_query. It calibrates the similarity threshold on questions that are not
evaluated, then runs mlflow.genai.evaluate on the held-out EVAL set. GPT-OSS-120B answers with
[report_id] citations that code checks; a Llama 3.3 judge (a different model family) scores
correctness, groundedness and relevance. Every question is one MLflow trace.
"""
import argparse
import json
import re
import sys
import time

parser = argparse.ArgumentParser()
parser.add_argument("--catalog", required=True)
parser.add_argument("--embedding-endpoint", required=True)
parser.add_argument("--chat-endpoint", required=True)
parser.add_argument("--judge-endpoint", required=True)
parser.add_argument("--dimensions", type=int, required=True)
parser.add_argument("--k", type=int, required=True)
parser.add_argument("--max-tokens", type=int, required=True)
parser.add_argument("--reasoning-effort", required=True)
parser.add_argument("--workers", type=int, required=True)
parser.add_argument("--job-run-id", required=True)
parser.add_argument("--source-root", required=True)
args = parser.parse_args()
# Serverless Python tasks execute via exec(), where __file__ is not defined.
sys.path.insert(0, args.source_root)

import mlflow
import numpy as np
from pyspark.sql import SparkSession, functions as F

from sentinelops import answer_eval, retrieval_eval
from sentinelops.answers import DBU_PER_MILLION_TOKENS, PROMPT_VERSION, SYSTEM_PROMPT, Assistant, ChatClient
from sentinelops.embeddings import format_query, unit_vectors
from sentinelops.retrieval import ExactIndex, truncate

endpoints = [args.embedding_endpoint, args.chat_endpoint, args.judge_endpoint]
if not re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_]*", args.catalog) or not all(re.fullmatch(r"[a-z0-9-]+", e) for e in endpoints):
    raise ValueError("Catalog must be a simple identifier and endpoints serving endpoint names")
if not re.fullmatch(r"[0-9]+", args.job_run_id) or args.reasoning_effort not in ("low", "medium", "high"):
    raise ValueError("Job run ID must be numeric and reasoning effort low, medium or high")

started = time.monotonic()
spark = SparkSession.builder.getOrCreate()
gold = f"{args.catalog}.gold"
documents = spark.table(f"{gold}.osha_documents").select("report_id", "document", "document_sha256")
# Only embeddings of the current document text count; the prefix is truncated to unit length below.
prefixes = spark.table(f"{gold}.osha_embeddings").filter(F.col("model") == args.embedding_endpoint) \
    .select("report_id", "document_sha256", F.slice("embedding", 1, args.dimensions).alias("prefix"))
frame = documents.join(prefixes, ["report_id", "document_sha256"]).orderBy("report_id").toPandas()
if len(frame) != documents.count():
    raise ValueError(f"{documents.count() - len(frame)} documents lack a current embedding; run osha_embed first")
ids = frame.report_id.to_numpy()
index = ExactIndex(ids, truncate(np.stack(frame.prefix.to_numpy()), args.dimensions))
texts = dict(zip(ids.tolist(), frame.document))
load_seconds = time.monotonic() - started


def dev(category):
    return [q["question"] for q in answer_eval.DEV if q["category"] == category]


# Threshold calibration never sees EVAL questions: retrieval-eval paraphrases and DEV questions only.
on_topic = [q["question"] for q in retrieval_eval.QUESTIONS] + dev(answer_eval.ANSWERABLE)
off_topic = [q["question"] for q in retrieval_eval.OFF_TOPIC] + dev(answer_eval.OFF_TOPIC)
dev_unanswerable = dev(answer_eval.UNANSWERABLE)
eval_questions = [q["question"] for q in answer_eval.EVAL]
asked = list(dict.fromkeys(on_topic + off_topic + dev_unanswerable + eval_questions))
query_frame = spark.createDataFrame([(i, format_query(q)) for i, q in enumerate(asked)], "i INT, text STRING")
embedded = {row.i: row.e for row in query_frame.selectExpr("i", f"ai_query('{args.embedding_endpoint}', text) AS e").collect()}
vectors = dict(zip(asked, unit_vectors([embedded[i] for i in range(len(asked))])))


def top1(items):
    _, scores = index.search(truncate(np.stack([vectors[q] for q in items]), args.dimensions), 1)
    return [round(float(s), 4) for s in scores[:, 0]]


on_scores, off_scores, dev_unanswerable_scores = top1(on_topic), top1(off_topic), top1(dev_unanswerable)
threshold = answer_eval.calibrate_threshold(on_scores, off_scores)
calibration = {"threshold": threshold, "rule": "midpoint of min on-topic and max off-topic top-1 cosine",
               "on_topic": {"questions": len(on_scores), "min": min(on_scores), "median": float(np.median(on_scores))},
               "off_topic": {"questions": len(off_scores), "max": max(off_scores)},
               "dev_unanswerable": {"questions": len(dev_unanswerable_scores), "min": min(dev_unanswerable_scores),
                                    "max": max(dev_unanswerable_scores),
                                    "below_threshold": sum(s < threshold for s in dev_unanswerable_scores)}}

chat = ChatClient.from_workspace(args.chat_endpoint, max_tokens=args.max_tokens, reasoning_effort=args.reasoning_effort)
assistant = Assistant(index, texts, embed_query=vectors.__getitem__, chat=chat, min_score=threshold, k=args.k,
                      model=args.chat_endpoint)

user = spark.sql("SELECT current_user()").first()[0]
mlflow.set_experiment(f"/Users/{user}/sentinelops-safety-rag")
with mlflow.start_run(run_name=f"answer-eval-{answer_eval.VERSION}") as run:
    mlflow.log_params({"eval_version": answer_eval.VERSION, "eval_fingerprint": answer_eval.fingerprint(),
                       "prompt_version": PROMPT_VERSION, "chat_model": args.chat_endpoint,
                       "judge_model": args.judge_endpoint, "embedding_model": args.embedding_endpoint,
                       "dimensions": args.dimensions, "k": args.k, "max_tokens": args.max_tokens,
                       "reasoning_effort": args.reasoning_effort, "temperature": 0.0, "min_score": threshold,
                       "documents": len(frame), "questions": len(eval_questions), "workers": args.workers,
                       "job_run_id": args.job_run_id})
    mlflow.log_text(SYSTEM_PROMPT, "system_prompt.txt")
    mlflow.log_dict(calibration, "calibration.json")
    eval_started = time.monotonic()
    result, rows = answer_eval.evaluate_assistant(assistant, answer_eval.EVAL, judge_model=f"databricks:/{args.judge_endpoint}",
                                                  workers=args.workers)
    eval_seconds = time.monotonic() - eval_started
    summary = answer_eval.summarize(rows)
    generation_dbus = (summary["overall"]["input_tokens"] * DBU_PER_MILLION_TOKENS["input"]
                       + summary["overall"]["output_tokens"] * DBU_PER_MILLION_TOKENS["output"]) / 1e6
    mlflow.log_metrics({f"{group}_{name}": float(value) for group, values in summary.items()
                        for name, value in values.items() if isinstance(value, (int, float))})
    mlflow.log_metrics({"min_score": threshold, "generation_dbus": generation_dbus, "index_load_seconds": load_seconds,
                        "evaluate_seconds": eval_seconds})
    per_question = [{key: row[key] for key in ("question_id", "category", "status", "top1_score", "judges", "answer",
                                               "trace_id")}
                    | {"cited_ids": (row["citations"] or {}).get("cited_ids"),
                       "coverage": (row["citations"] or {}).get("coverage")} for row in rows]
    report = {"mlflow_run_id": run.info.run_id, "eval_fingerprint": answer_eval.fingerprint(),
              "prompt_version": PROMPT_VERSION, "documents": len(frame), "index_mb": round(index.megabytes, 1),
              "calibration": calibration, "summary": summary, "generation_dbus": round(generation_dbus, 5),
              "seconds": {"index_load": round(load_seconds, 1), "evaluate": round(eval_seconds, 1),
                          "total": round(time.monotonic() - started, 1)},
              "per_question": sorted(per_question, key=lambda r: (r["category"], r["question_id"]))}
    mlflow.log_dict(report, "answer_report.json")
print(json.dumps(report, default=str))
