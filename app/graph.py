"""The review graph.

    START -> extract_clauses -> score_risk --(any finding >= threshold)--> human_review -> draft_remediation -> finalize -> END
                                         \\--------------(otherwise)-----------------------/

`human_review` calls `interrupt()`, which saves a checkpoint and returns control
to the caller with the findings that need a decision. The run stays paused,
in Postgres, until someone resumes it with `Command(resume=[decisions])`.
"""

from __future__ import annotations

import time
from typing import Literal

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from app.analyzers import Analyzer, Usage, default_analyzer
from app.config import settings
from app.state import RISK_ORDER, Finding, NodeMetric, ReviewDecision, ReviewState


def _metric(node: str, started: float, *usages: Usage) -> NodeMetric:
    return NodeMetric(
        node=node,
        latency_ms=round((time.perf_counter() - started) * 1000, 2),
        input_tokens=sum(u.input_tokens for u in usages),
        output_tokens=sum(u.output_tokens for u in usages),
        cost_usd=round(sum(u.cost_usd for u in usages), 6),
    )


def _needs_review(finding: Finding, threshold: str) -> bool:
    return RISK_ORDER[finding.risk] >= RISK_ORDER[threshold]


def build_graph(analyzer: Analyzer | None = None, checkpointer=None, review_threshold: str | None = None):
    analyzer = analyzer or default_analyzer()
    threshold = review_threshold or settings.review_threshold

    def extract_clauses(state: ReviewState) -> dict:
        started = time.perf_counter()
        clauses, usage = analyzer.extract_clauses(state["document_text"])
        return {
            "clauses": clauses,
            "status": "running",
            "metrics": [_metric("extract_clauses", started, usage)],
            "audit_log": [f"extracted {len(clauses)} clauses using {analyzer.name}"],
        }

    def score_risk(state: ReviewState) -> dict:
        started = time.perf_counter()
        findings, usage = analyzer.score_risk(state["clauses"])
        for f in findings:
            if _needs_review(f, threshold):
                f.status = "pending_review"
        flagged = sum(f.status == "pending_review" for f in findings)
        return {
            "findings": findings,
            "metrics": [_metric("score_risk", started, usage)],
            "audit_log": [f"scored {len(findings)} findings, {flagged} flagged for human review"],
        }

    def route_after_scoring(state: ReviewState) -> Literal["human_review", "draft_remediation"]:
        return "human_review" if any(f.status == "pending_review" for f in state["findings"]) else "draft_remediation"

    def human_review(state: ReviewState) -> dict:
        # Note: on resume LangGraph re-runs this node from the top, replaying
        # earlier interrupt() answers in order. Keep everything before and
        # between interrupts deterministic and side-effect free.
        started = time.perf_counter()
        findings = [f.model_copy() for f in state["findings"]]
        clause_text = {c.id: c.text for c in state["clauses"]}
        log: list[str] = []

        while pending := [f for f in findings if f.status == "pending_review"]:
            answer = interrupt({
                "doc_id": state.get("doc_id"),
                "message": f"{len(pending)} finding(s) need a reviewer decision.",
                "findings": [
                    {**f.model_dump(include={"clause_id", "category", "risk", "rationale"}),
                     "clause_text": clause_text.get(f.clause_id, "")}
                    for f in pending
                ],
            })
            payload = answer.get("decisions", []) if isinstance(answer, dict) else answer
            reviewer = answer.get("reviewer", "unknown") if isinstance(answer, dict) else "unknown"
            decisions = {d.clause_id: d for d in (ReviewDecision.model_validate(x) for x in payload)}

            for f in pending:
                d = decisions.get(f.clause_id)
                if d is None:
                    continue  # still pending; we'll interrupt again for these
                if d.action == "approve":
                    f.status = "approved"
                elif d.action == "reject":
                    f.status = "rejected"
                else:
                    f.status = "edited"
                    f.risk = d.risk or f.risk
                f.reviewer_note = d.note
                past = {"approve": "approved", "edit": "edited", "reject": "rejected"}[d.action]
                log.append(f"{reviewer} {past} {f.clause_id}" + (f" (risk -> {f.risk})" if d.action == "edit" else ""))

        return {
            "findings": findings,
            "metrics": [_metric("human_review", started)],
            "audit_log": log,
        }

    def draft_remediation(state: ReviewState) -> dict:
        started = time.perf_counter()
        clauses = {c.id: c for c in state["clauses"]}
        findings = [f.model_copy() for f in state["findings"]]
        usages: list[Usage] = []
        drafted = 0
        for f in findings:
            # Rejected = reviewer says it's a false positive; low risk needs no markup.
            if f.status == "rejected" or f.risk == "low":
                continue
            f.remediation, usage = analyzer.draft_remediation(f, clauses[f.clause_id])
            usages.append(usage)
            drafted += 1
        return {
            "findings": findings,
            "metrics": [_metric("draft_remediation", started, *usages)],
            "audit_log": [f"drafted {drafted} remediation note(s)"],
        }

    def finalize(state: ReviewState) -> dict:
        findings = state["findings"]
        metrics = state.get("metrics", [])
        report = {
            "doc_id": state.get("doc_id"),
            "summary": {
                "clauses": len(state["clauses"]),
                "by_risk": {r: sum(f.risk == r for f in findings if f.status != "rejected") for r in RISK_ORDER},
                "human_reviewed": sum(f.status in {"approved", "edited", "rejected"} for f in findings),
                "rejected_as_false_positive": sum(f.status == "rejected" for f in findings),
            },
            "cost_usd": round(sum(m["cost_usd"] for m in metrics), 6),
            "latency_ms": round(sum(m["latency_ms"] for m in metrics if m["node"] != "human_review"), 2),
            "findings": [f.model_dump() for f in findings if f.risk != "low" and f.status != "rejected"],
        }
        return {"report": report, "status": "completed", "audit_log": ["report finalised"]}

    builder = StateGraph(ReviewState)
    builder.add_node("extract_clauses", extract_clauses)
    builder.add_node("score_risk", score_risk)
    builder.add_node("human_review", human_review)
    builder.add_node("draft_remediation", draft_remediation)
    builder.add_node("finalize", finalize)

    builder.add_edge(START, "extract_clauses")
    builder.add_edge("extract_clauses", "score_risk")
    builder.add_conditional_edges("score_risk", route_after_scoring)
    builder.add_edge("human_review", "draft_remediation")
    builder.add_edge("draft_remediation", "finalize")
    builder.add_edge("finalize", END)

    return builder.compile(checkpointer=checkpointer)
