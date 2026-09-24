import mlflow
import numpy as np
import pandas as pd
import pytest

from sentinelops import answer_eval
from sentinelops.answer_eval import (DEV, EVAL_V1, EVAL_V2, SETS, by_set, calibrate_threshold, collect, dataset,
                                     decision_correct, decline_routes, fingerprint, summarize)
from sentinelops.answers import (ANSWERED, DECLINE_MARKER, DECLINED_BY_MODEL, DECLINED_LOW_SIMILARITY, ERROR,
                                 REJECTED_CITATIONS, Assistant, ChatClient, build_messages, check_citations,
                                 cited_ids, decline_message, message_text, normalize_citations, sentences)
from sentinelops.retrieval import ExactIndex
from sentinelops.retrieval_eval import OFF_TOPIC as RETRIEVAL_OFF_TOPIC, QUESTIONS as RETRIEVAL_QUESTIONS


@pytest.fixture(scope="module", autouse=True)
def local_tracking(tmp_path_factory):
    """Traces go to a throwaway store; the experiment's artifacts stay out of the working tree."""
    root = tmp_path_factory.mktemp("mlflow")
    previous = mlflow.get_tracking_uri()
    mlflow.set_tracking_uri(f"sqlite:///{(root / 'mlflow.db').as_posix()}")
    mlflow.set_experiment(experiment_id=mlflow.create_experiment("answers", artifact_location=root.as_uri()))
    yield
    mlflow.set_tracking_uri(previous)


def reply(text, finish_reason="stop"):
    return {"text": text, "reasoning": "", "finish_reason": finish_reason, "input_tokens": 100, "output_tokens": 20,
            "attempts": 1}


def assistant(chat, min_score=0.5):
    """Three orthogonal 4-d documents; the question vector points at document 11 (score 1.0)."""
    index = ExactIndex(np.array([10, 11, 12]), np.eye(4, dtype=np.float32)[:3])
    vectors = {"on": np.array([0, 2, 0, 0], dtype=np.float32), "off": np.array([0, 0, 0, 1], dtype=np.float32)}
    return Assistant(index, {10: "doc ten", 11: "doc eleven", 12: "doc twelve"}, embed_query=vectors.__getitem__,
                     chat=chat, min_score=min_score, k=2)


def test_message_text_handles_strings_and_reasoning_blocks():
    assert message_text("plain") == ("plain", "")
    blocks = [{"type": "reasoning", "summary": [{"type": "summary_text", "text": "think"}]},
              {"type": "text", "text": "A stop sign is red."}]
    assert message_text(blocks) == ("A stop sign is red.", "think")
    assert message_text(None) == ("", "")


def test_citations_are_parsed_and_full_width_brackets_normalized():
    assert cited_ids("Hands [931176][930267]. Arms [1, 22 ,333].") == [931176, 930267, 1, 22, 333]
    assert cited_ids("No citations [a1] or (123).") == []
    assert normalize_citations("Crushed【1296640】【1555027】.") == "Crushed[1296640][1555027]."


def test_sentences_skip_list_intros_and_keep_trailing_citations_attached():
    text = "Common causes:\n- Hands caught [1].\n- Falls. [2] Burns were rare.\n2. Eyes [3]."
    assert sentences(text) == ["Hands caught [1].", "Falls. [2]", "Burns were rare.", "Eyes [3]."]


def test_check_citations_requires_a_citation_and_only_retrieved_ids():
    ok = check_citations("Workers were caught [11]. Some lost fingers [11][12].", [10, 11, 12])
    assert ok["valid"] and ok["coverage"] == 1.0 and ok["cited_ids"] == [11, 12]
    partial = check_citations("Workers were caught [11]. Some lost fingers.", [11])
    assert partial["valid"] and partial["coverage"] == 0.5 and partial["uncited_sentences"] == ["Some lost fingers."]
    invented = check_citations("Workers were caught [99].", [10, 11])
    assert not invented["valid"] and invented["invalid_ids"] == [99]
    assert not check_citations("Workers were caught.", [10])["valid"]


def test_decline_message_uses_the_models_reason():
    assert decline_message(f"{DECLINE_MARKER} – the reports have no penalty amounts.") == \
        "I can't answer that from the OSHA severe injury reports. The reports have no penalty amounts."
    assert decline_message(DECLINE_MARKER).endswith("don't contain that information.")


def test_prompt_labels_every_report_with_its_id():
    messages = build_messages("Why?", [(7, "first"), (8, "second")])
    assert messages[0]["role"] == "system" and DECLINE_MARKER in messages[0]["content"]
    assert '<report id="7">\nfirst\n</report>' in messages[1]["content"] and messages[1]["content"].startswith("Question: Why?")


class FakeResponse:
    def __init__(self, status_code, body=None, headers=None):
        self.status_code, self.body, self.headers, self.text = status_code, body, headers or {}, str(body)

    def json(self):
        return self.body


def test_chat_client_retries_throttling_visibly_and_parses_the_reply():
    body = {"choices": [{"message": {"content": [{"type": "reasoning", "summary": [{"text": "r"}]},
                                                  {"type": "text", "text": " Answer [1]. "}]},
                         "finish_reason": "stop"}], "usage": {"prompt_tokens": 50, "completion_tokens": 9}}
    responses, calls, sleeps = [FakeResponse(429, headers={"Retry-After": "3"}), FakeResponse(503), FakeResponse(200, body)], [], []

    def post(url, headers, json, timeout):
        calls.append((url, headers, json))
        return responses.pop(0)

    client = ChatClient("https://host/", lambda: {"Authorization": "Bearer x"}, "chat", max_tokens=64,
                        post=post, sleep=sleeps.append, backoff_seconds=1)
    out = client([{"role": "user", "content": "q"}])
    assert out == {"text": "Answer [1].", "reasoning": "r", "finish_reason": "stop", "input_tokens": 50,
                   "output_tokens": 9, "attempts": 3}
    assert sleeps == [3.0, 2] and calls[0][0] == "https://host/serving-endpoints/chat/invocations"
    assert calls[0][2]["temperature"] == 0.0 and calls[0][2]["reasoning_effort"] == "low" and calls[0][2]["max_tokens"] == 64


def test_chat_client_fails_fast_on_client_errors():
    client = ChatClient("https://host", dict, "chat", post=lambda *a, **k: FakeResponse(400, "bad"), sleep=None)
    with pytest.raises(RuntimeError, match="HTTP 400"):
        client([])


def test_assistant_declines_below_threshold_without_calling_the_model():
    def chat(messages):
        raise AssertionError("must not be called")
    out = assistant(chat).answer("off")
    assert out["custom_outputs"]["status"] == DECLINED_LOW_SIMILARITY and out["custom_outputs"]["top1_score"] == 0.0


@pytest.mark.parametrize("text, status", [
    ("Workers were caught [11]. Some lost fingers【11】【10】.", ANSWERED),
    (f"{DECLINE_MARKER} The reports have no fines.", DECLINED_BY_MODEL),
    ("Workers were caught [99].", REJECTED_CITATIONS),
    ("Workers were caught.", REJECTED_CITATIONS),
])
def test_assistant_decisions(text, status):
    out = assistant(lambda messages: reply(text)).answer("on")
    custom = out["custom_outputs"]
    assert custom["status"] == status and custom["retrieved_ids"][0] == 11 and custom["top1_score"] == 1.0
    shown = out["output"][0]["content"][0]["text"]
    if status == ANSWERED:
        assert shown == "Workers were caught [11]. Some lost fingers[11][10]." and custom["citations"]["valid"]
    else:
        assert "[99]" not in shown and "caught" not in shown  # Rejected drafts are never shown.


def test_assistant_turns_model_failures_and_truncation_into_errors():
    def broken(messages):
        raise RuntimeError("HTTP 503")
    assert assistant(broken).answer("on")["custom_outputs"]["status"] == ERROR
    truncated = assistant(lambda m: reply("Workers [11]", finish_reason="length")).answer("on")["custom_outputs"]
    assert truncated["status"] == ERROR and truncated["error"] == "finish_reason=length"


def test_trace_has_retriever_documents_for_groundedness_judges():
    assistant(lambda messages: reply("Workers were caught [11].")).answer("on")
    mlflow.flush_trace_async_logging()
    trace = mlflow.get_trace(mlflow.get_last_active_trace_id())
    spans = {span.name: span for span in trace.data.spans}
    assert set(spans) == {"osha_answer", "retrieve", "generate", "check"}
    assert spans["retrieve"].span_type == "RETRIEVER" and spans["generate"].span_type == "CHAT_MODEL"
    assert spans["retrieve"].outputs[0] == {"page_content": "doc eleven",
                                            "metadata": {"doc_uri": "osha-sir:11", "report_id": 11, "score": 1.0}}
    assert spans["generate"].attributes["mlflow.chat.tokenUsage"]["total_tokens"] == 120
    assert trace.info.tags["sentinelops.status"] == ANSWERED


def test_question_sets_are_disjoint_labelled_and_fingerprinted():
    ids = [q["id"] for q in DEV + EVAL_V1 + EVAL_V2]
    assert len(ids) == len(set(ids))
    seen = {q["question"] for q in DEV + RETRIEVAL_QUESTIONS + RETRIEVAL_OFF_TOPIC}
    for questions in (EVAL_V1, EVAL_V2):  # each held-out set is new text, v2 also new to v1
        texts = {q["question"] for q in questions}
        assert len(texts) == len(questions) and not texts & seen
        seen |= texts
    for q in DEV + EVAL_V1 + EVAL_V2:
        assert (q["category"] == answer_eval.ANSWERABLE) == bool(q.get("facts"))
        assert (q["category"] == answer_eval.UNANSWERABLE) == bool(q.get("missing"))
    # The Llama judge reads "A or B" as both required, so v2 facts are single claims.
    assert not [f for q in EVAL_V2 for f in q.get("facts", []) if " or " in f]
    assert len(dataset(DEV)) == len(DEV)
    for questions in SETS.values():
        counts = pd.Series([q["category"] for q in questions]).value_counts()
        assert counts.min() >= 5 and len(counts) == 3
    assert len(EVAL_V2) == 60 and answer_eval.HELD_OUT == "eval_v2"
    assert len(fingerprint()) == 12


def test_dataset_rows_carry_expectations_for_each_category():
    assert {r["expectations"]["question_set"] for r in dataset()} == {"eval_v2"}
    rows = {r["expectations"]["question_id"]: r for r in dataset(EVAL_V1, "eval_v1")}
    assert rows["wood_chipper"]["expectations"]["question_set"] == "eval_v1"
    assert rows["wood_chipper"]["expectations"]["expected_facts"] and rows["wood_chipper"]["expectations"]["should_answer"]
    assert rows["sue_employer"]["expectations"]["missing"] == "legal advice"
    assert rows["sue_employer"]["expectations"]["expected_response"] == answer_eval.DECLINE_RESPONSE
    assert not rows["world_cup"]["expectations"]["should_answer"]
    assert rows["world_cup"]["inputs"] == {"question": "Who won the 2018 FIFA World Cup?"}


def test_threshold_is_the_midpoint_and_refuses_overlap():
    assert calibrate_threshold([0.71, 0.9], [0.3, 0.51]) == pytest.approx(0.61)
    with pytest.raises(ValueError):
        calibrate_threshold([0.5, 0.9], [0.6])


def test_decisions_and_summary():
    assert decision_correct(ANSWERED, True) and decision_correct(DECLINED_BY_MODEL, False)
    assert not decision_correct(REJECTED_CITATIONS, True) and not decision_correct(ANSWERED, False)
    with pytest.raises(ValueError):
        decision_correct("maybe", True)
    check = {"valid": True, "coverage": 0.5}
    rows = [{"question_id": "a", "category": "answerable", "status": ANSWERED, "citations": check,
             "judges": {"correctness": "yes", "retrieval_groundedness": "no"}, "input_tokens": 10, "output_tokens": 2},
            {"question_id": "b", "category": "answerable", "status": REJECTED_CITATIONS,
             "citations": {"valid": False, "coverage": 0.0}, "judges": {"correctness": None}, "input_tokens": 5},
            {"question_id": "c", "category": "off_topic", "status": DECLINED_LOW_SIMILARITY, "citations": None,
             "judges": {}}]
    summary = summarize(rows)
    answerable = summary["answerable"]
    assert answerable["decision_accuracy"] == 0.5 and answerable["wrong_decisions"] == ["b"]
    assert answerable["draft_citation_valid_rate"] == 0.5 and answerable["mean_citation_coverage"] == 0.25
    assert answerable["correctness_pass_rate"] == 1.0 and answerable["correctness_scored"] == 1
    assert answerable["retrieval_groundedness_pass_rate"] == 0.0
    assert summary["off_topic"]["decision_accuracy"] == 1.0 and "unanswerable" not in summary
    assert summary["overall"] == {"questions": 3, "decision_accuracy": pytest.approx(2 / 3), "input_tokens": 15,
                                  "output_tokens": 2}


def test_collect_reads_evaluate_results_and_counts_failed_predictions_as_errors():
    output = {"output": [{"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "A [1]."}]}],
              "custom_outputs": {"status": ANSWERED, "top1_score": 0.8, "citations": {"valid": True, "coverage": 1.0},
                                 "usage": {"input_tokens": 7, "output_tokens": 3}}}
    frame = pd.DataFrame([
        {"trace_id": "t1", "question_id/value": "q1", "category/value": "answerable", "response": output,
         "correctness/value": "yes", "retrieval_groundedness/value": np.nan},
        {"trace_id": "t2", "question_id/value": "q2", "category/value": "off_topic", "response": None,
         "correctness/value": "no", "retrieval_groundedness/value": "yes"}])
    first, second = collect(frame)
    assert first["answer"] == "A [1]." and first["status"] == ANSWERED and first["input_tokens"] == 7
    assert first["judges"] == {"correctness": "yes", "retrieval_groundedness": None}
    assert second["status"] == ERROR and second["answer"] is None and second["judges"]["retrieval_groundedness"] == "yes"


def test_decline_routes_separate_threshold_declines_from_model_declines():
    rows = [{"question_id": "u1", "category": "unanswerable", "status": DECLINED_LOW_SIMILARITY, "top1_score": 0.60},
            {"question_id": "u2", "category": "unanswerable", "status": DECLINED_BY_MODEL, "top1_score": 0.70},
            {"question_id": "u3", "category": "unanswerable", "status": ANSWERED, "top1_score": 0.66},
            {"question_id": "o1", "category": "off_topic", "status": DECLINED_LOW_SIMILARITY, "top1_score": 0.40},
            {"question_id": "a1", "category": "answerable", "status": ANSWERED, "top1_score": 0.80}]
    routes = decline_routes(rows, 0.65)
    assert routes["unanswerable"] == {"questions": 3, "below_threshold": 1, "above_threshold": 2,
                                      "above_threshold_declined_by_model": 1, "answered_wrongly": ["u3"],
                                      "within_0_02_of_threshold": ["u3"]}
    assert routes["off_topic"]["below_threshold"] == 1 and "answerable" not in routes
    split = by_set([{**r, "question_set": "eval_v2"} for r in rows[:3]] + [{**rows[4], "question_set": "eval_v1"}], 0.65)
    assert list(split) == ["eval_v2", "eval_v1"]
    assert split["eval_v2"]["summary"]["unanswerable"]["wrong_decisions"] == ["u3"]
    assert split["eval_v1"]["summary"]["answerable"]["decision_accuracy"] == 1.0