"""Offline eval: run every golden contract end to end and score the agent.

Each run goes through the real graph, including the human-review interrupt
(auto-approved here, so the eval measures the model, not the reviewer).

Metrics
- risk_accuracy:   share of labelled clauses where predicted risk == expected
- high_recall:     share of truly high-risk clauses the agent flagged high
- high_precision:  share of clauses flagged high that really are high
- cost_usd / latency_ms: totals and p95 per document, from per-node metrics

Usage:
    python -m evals.run_evals                     # rule baseline (no API key)
    LLM_PROVIDER=ollama python -m evals.run_evals --out evals/results/llm.json
    python -m evals.run_evals --min-high-recall 0.8   # fails CI below threshold

If Langfuse keys are set, each document's scores are attached to its session
so they sit next to the traces in the Langfuse UI.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import uuid
from pathlib import Path

from langgraph.types import Command

from app.analyzers import default_analyzer
from app.config import settings
from app.graph import build_graph
from app.infra import make_checkpointer, tracing_callbacks

GOLDEN = Path(__file__).parent / "golden" / "contracts.json"


def run_document(graph, case: dict) -> dict:
    thread_id = f"eval-{case['doc_id']}-{uuid.uuid4().hex[:8]}"
    callbacks, metadata = tracing_callbacks(session_id=thread_id, user_id="eval")
    cfg = {"configurable": {"thread_id": thread_id}, "callbacks": callbacks, "metadata": metadata}

    out = graph.invoke({"doc_id": case["doc_id"], "document_text": case["document_text"]}, cfg)
    while "__interrupt__" in out:
        pending = out["__interrupt__"][0].value["findings"]
        decisions = [{"clause_id": f["clause_id"], "action": "approve"} for f in pending]
        out = graph.invoke(Command(resume={"reviewer": "eval-bot", "decisions": decisions}), cfg)

    clause_text = {c.id: c.text for c in out["clauses"]}
    predicted_by_clause = {f.clause_id: f.risk for f in out["findings"]}

    rows = []
    for exp in case["expected"]:
        match = next((cid for cid, text in clause_text.items() if exp["contains"].lower() in text.lower()), None)
        rows.append({
            "contains": exp["contains"],
            "expected": exp["risk"],
            "predicted": predicted_by_clause.get(match, "missing") if match else "missing",
        })

    metrics = out.get("metrics", [])
    return {
        "doc_id": case["doc_id"],
        "thread_id": thread_id,
        "rows": rows,
        "cost_usd": sum(m["cost_usd"] for m in metrics),
        "latency_ms": sum(m["latency_ms"] for m in metrics if m["node"] != "human_review"),
        "tokens": sum(m["input_tokens"] + m["output_tokens"] for m in metrics),
    }


def summarise(results: list[dict]) -> dict:
    rows = [r for res in results for r in res["rows"]]
    correct = sum(r["expected"] == r["predicted"] for r in rows)
    true_high = [r for r in rows if r["expected"] == "high"]
    pred_high = [r for r in rows if r["predicted"] == "high"]
    latencies = sorted(res["latency_ms"] for res in results)
    p95_idx = max(0, int(round(0.95 * len(latencies))) - 1)
    return {
        "documents": len(results),
        "labelled_clauses": len(rows),
        "risk_accuracy": round(correct / len(rows), 3),
        "high_recall": round(sum(r["predicted"] == "high" for r in true_high) / len(true_high), 3) if true_high else None,
        "high_precision": round(sum(r["expected"] == "high" for r in pred_high) / len(pred_high), 3) if pred_high else None,
        "total_cost_usd": round(sum(res["cost_usd"] for res in results), 6),
        "cost_per_doc_usd": round(statistics.mean(res["cost_usd"] for res in results), 6),
        "mean_latency_ms": round(statistics.mean(latencies), 2),
        "p95_latency_ms": round(latencies[p95_idx], 2),
        "total_tokens": sum(res["tokens"] for res in results),
    }


def push_to_langfuse(results: list[dict]) -> None:
    if not settings.langfuse_enabled:
        return
    from langfuse import Langfuse

    lf = Langfuse()
    for res in results:
        acc = sum(r["expected"] == r["predicted"] for r in res["rows"]) / len(res["rows"])
        lf.create_score(name="risk_accuracy", value=acc, session_id=res["thread_id"], data_type="NUMERIC")
        lf.create_score(name="cost_usd", value=res["cost_usd"], session_id=res["thread_id"], data_type="NUMERIC")
    lf.flush()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, help="write full results JSON here")
    parser.add_argument("--min-high-recall", type=float, default=None, help="exit 1 if high-risk recall is below this")
    args = parser.parse_args()

    analyzer = default_analyzer()
    graph = build_graph(analyzer=analyzer, checkpointer=make_checkpointer())
    cases = json.loads(GOLDEN.read_text())
    results = [run_document(graph, case) for case in cases]
    summary = {"analyzer": analyzer.name, **summarise(results)}

    print(json.dumps(summary, indent=2))
    misses = [(res["doc_id"], r) for res in results for r in res["rows"] if r["expected"] != r["predicted"]]
    if misses:
        print("\nMismatches:")
        for doc_id, r in misses:
            print(f"  {doc_id:20} {r['contains']!r:32} expected={r['expected']:6} predicted={r['predicted']}")

    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps({"summary": summary, "results": results}, indent=2))
    push_to_langfuse(results)

    if args.min_high_recall is not None and (summary["high_recall"] or 0) < args.min_high_recall:
        print(f"\nFAIL: high_recall {summary['high_recall']} < {args.min_high_recall}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
