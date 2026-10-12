"""Typed graph state.

Every node reads from and returns a partial update to ReviewState. Lists that
several nodes contribute to use an `operator.add` reducer so updates append
rather than overwrite. Because the whole state is checkpointed after each
node, a run can pause for a human, survive a container restart, and be
audited later from the checkpoint history.
"""

from __future__ import annotations

import operator
from typing import Annotated, Literal, TypedDict

from pydantic import BaseModel, Field

RiskLevel = Literal["low", "medium", "high"]
RISK_ORDER: dict[str, int] = {"low": 0, "medium": 1, "high": 2}


class Clause(BaseModel):
    id: str
    text: str
    category: str = Field(description="e.g. liability, data_protection, termination, payment, confidentiality, other")


class Finding(BaseModel):
    clause_id: str
    category: str
    risk: RiskLevel
    rationale: str
    # Filled in by the human review step.
    status: Literal["auto_accepted", "pending_review", "approved", "edited", "rejected"] = "auto_accepted"
    reviewer_note: str | None = None
    remediation: str | None = None


class ReviewDecision(BaseModel):
    """What a reviewer sends back to resume a paused run."""

    clause_id: str
    action: Literal["approve", "edit", "reject"]
    risk: RiskLevel | None = None  # only used with action="edit"
    note: str | None = None


class NodeMetric(TypedDict):
    node: str
    latency_ms: float
    input_tokens: int
    output_tokens: int
    cost_usd: float


class ReviewState(TypedDict, total=False):
    doc_id: str
    document_text: str
    clauses: list[Clause]
    findings: list[Finding]
    reviewer: str | None
    report: dict
    status: Literal["running", "awaiting_review", "completed"]
    # Appended to by every node; drives cost/latency reporting and evals.
    metrics: Annotated[list[NodeMetric], operator.add]
    audit_log: Annotated[list[str], operator.add]
