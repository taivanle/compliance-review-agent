"""Graph and API tests. Run offline against the rule-based analyzer."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from langgraph.types import Command

from app.analyzers import RuleBasedAnalyzer
from app.api import create_app
from app.graph import build_graph
from app.infra import make_serde

RISKY_DOC = """1. The Supplier shall have unlimited liability for any breach.

2. The Supplier may transfer personal data to servers outside the UK.

3. Invoices are payable within 30 days.

4. This Agreement shall automatically renew each year."""

CLEAN_DOC = """1. Invoices are payable within 30 days.

2. This Agreement is governed by the laws of England and Wales."""


@pytest.fixture
def graph():
    from langgraph.checkpoint.memory import InMemorySaver

    return build_graph(analyzer=RuleBasedAnalyzer(), checkpointer=InMemorySaver(serde=make_serde()))


def cfg(thread: str) -> dict:
    return {"configurable": {"thread_id": thread}}


# ---------------------------------------------------------------- graph

def test_high_risk_pauses_for_human(graph):
    out = graph.invoke({"doc_id": "d", "document_text": RISKY_DOC}, cfg("t1"))
    assert "__interrupt__" in out
    pending = out["__interrupt__"][0].value["findings"]
    assert {f["clause_id"] for f in pending} == {"c1", "c2"}
    assert graph.get_state(cfg("t1")).next == ("human_review",)


def test_clean_document_skips_review(graph):
    out = graph.invoke({"doc_id": "d", "document_text": CLEAN_DOC}, cfg("t2"))
    assert "__interrupt__" not in out
    assert out["status"] == "completed"
    assert out["report"]["summary"]["human_reviewed"] == 0


def test_resume_applies_decisions(graph):
    graph.invoke({"doc_id": "d", "document_text": RISKY_DOC}, cfg("t3"))
    out = graph.invoke(
        Command(resume={"reviewer": "owen", "decisions": [
            {"clause_id": "c1", "action": "approve"},
            {"clause_id": "c2", "action": "reject", "note": "transfer covered by IDTA"},
        ]}),
        cfg("t3"),
    )
    assert out["status"] == "completed"
    by_id = {f.clause_id: f for f in out["findings"]}
    assert by_id["c1"].status == "approved" and by_id["c1"].remediation
    assert by_id["c2"].status == "rejected" and by_id["c2"].remediation is None
    assert out["report"]["summary"]["rejected_as_false_positive"] == 1
    assert "owen rejected c2" in out["audit_log"]


def test_partial_decisions_interrupt_again(graph):
    graph.invoke({"doc_id": "d", "document_text": RISKY_DOC}, cfg("t4"))
    out = graph.invoke(Command(resume={"reviewer": "owen", "decisions": [{"clause_id": "c1", "action": "approve"}]}), cfg("t4"))
    assert [f["clause_id"] for f in out["__interrupt__"][0].value["findings"]] == ["c2"]
    out = graph.invoke(
        Command(resume={"reviewer": "owen", "decisions": [{"clause_id": "c2", "action": "edit", "risk": "medium"}]}),
        cfg("t4"),
    )
    assert out["status"] == "completed"
    assert next(f for f in out["findings"] if f.clause_id == "c2").risk == "medium"


def test_metrics_recorded_per_node(graph):
    out = graph.invoke({"doc_id": "d", "document_text": CLEAN_DOC}, cfg("t5"))
    nodes = [m["node"] for m in out["metrics"]]
    assert nodes == ["extract_clauses", "score_risk", "draft_remediation"]
    assert all(m["latency_ms"] >= 0 for m in out["metrics"])


def test_state_survives_new_graph_instance():
    """Same checkpointer, fresh graph object: simulates a container restart / another ECS task."""
    from langgraph.checkpoint.memory import InMemorySaver

    saver = InMemorySaver(serde=make_serde())
    build_graph(analyzer=RuleBasedAnalyzer(), checkpointer=saver).invoke({"doc_id": "d", "document_text": RISKY_DOC}, cfg("t6"))
    fresh = build_graph(analyzer=RuleBasedAnalyzer(), checkpointer=saver)
    out = fresh.invoke(Command(resume={"reviewer": "owen", "decisions": [
        {"clause_id": "c1", "action": "approve"}, {"clause_id": "c2", "action": "approve"}]}), cfg("t6"))
    assert out["status"] == "completed"


# ---------------------------------------------------------------- API

@pytest.fixture
def client(graph):
    with TestClient(create_app(graph=graph)) as c:
        yield c


def test_api_full_flow(client):
    r = client.post("/reviews", json={"doc_id": "msa-1", "document_text": RISKY_DOC})
    assert r.status_code == 201
    body = r.json()
    assert body["status"] == "awaiting_review"
    thread = body["thread_id"]

    assert client.get(f"/reviews/{thread}").json()["status"] == "awaiting_review"

    r = client.post(f"/reviews/{thread}/decisions", json={"reviewer": "owen", "decisions": [
        {"clause_id": "c1", "action": "approve"}, {"clause_id": "c2", "action": "approve"}]})
    assert r.status_code == 200
    assert r.json()["status"] == "completed"

    hist = client.get(f"/reviews/{thread}/history").json()
    assert len(hist["checkpoints"]) > 3
    assert any("owen approved" in line for line in hist["audit_log"])


def test_api_rejects_unknown_clause(client):
    thread = client.post("/reviews", json={"doc_id": "x", "document_text": RISKY_DOC}).json()["thread_id"]
    r = client.post(f"/reviews/{thread}/decisions", json={"reviewer": "owen", "decisions": [
        {"clause_id": "c3", "action": "approve"}]})
    assert r.status_code == 422


def test_api_conflict_when_completed(client):
    thread = client.post("/reviews", json={"doc_id": "x", "document_text": CLEAN_DOC}).json()["thread_id"]
    r = client.post(f"/reviews/{thread}/decisions", json={"reviewer": "owen", "decisions": [
        {"clause_id": "c1", "action": "approve"}]})
    assert r.status_code == 409


def test_api_404(client):
    assert client.get("/reviews/nope").status_code == 404
    assert client.get("/healthz").json() == {"ok": True}


# ---------------------------------------------------------------- LLM path (Ollama client, fake server)

def test_ollama_analyzer_end_to_end():
    from dataclasses import replace

    from langgraph.checkpoint.memory import InMemorySaver

    from app.analyzers import LLMAnalyzer
    from app.config import settings
    from tests.fake_ollama import FakeOllama

    with FakeOllama() as fake:
        llm_settings = replace(settings, llm_provider="ollama", ollama_base_url=fake.url, ollama_model="qwen2.5:7b",
                               input_cost_per_mtok=0.25, output_cost_per_mtok=1.25)
        analyzer = LLMAnalyzer(cfg=llm_settings)
        graph = build_graph(analyzer=analyzer, checkpointer=InMemorySaver(serde=make_serde()))

        out = graph.invoke({"doc_id": "d", "document_text": RISKY_DOC}, cfg("t-llm"))
        assert {f["clause_id"] for f in out["__interrupt__"][0].value["findings"]} == {"c1", "c2"}
        out = graph.invoke(Command(resume={"reviewer": "owen", "decisions": [
            {"clause_id": "c1", "action": "approve"}, {"clause_id": "c2", "action": "approve"}]}), cfg("t-llm"))

    assert analyzer.name == "ollama:qwen2.5:7b"
    assert out["status"] == "completed"
    llm_metrics = [m for m in out["metrics"] if m["node"] in {"extract_clauses", "score_risk"}]
    assert all(m["input_tokens"] > 0 and m["output_tokens"] > 0 for m in llm_metrics)
    assert out["report"]["cost_usd"] > 0  # priced at the reference rate we passed in
    assert fake.app.state.calls == 2 + 3  # extract + score + one remediation per non-low finding
