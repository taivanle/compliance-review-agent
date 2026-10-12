"""HTTP API.

POST /reviews                       start a review; runs until done or a human is needed
GET  /reviews/{thread_id}           current status, pending findings or final report
POST /reviews/{thread_id}/decisions submit reviewer decisions and resume the graph
GET  /reviews/{thread_id}/history   audit trail and checkpoint history
GET  /healthz                       ALB / ECS health check
"""

from __future__ import annotations

import logging
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from langgraph.types import Command
from pydantic import BaseModel, Field

from app.analyzers import default_analyzer
from app.graph import build_graph
from app.infra import make_checkpointer, tracing_callbacks
from app.state import ReviewDecision

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")


class StartReview(BaseModel):
    doc_id: str
    document_text: str = Field(min_length=1)


class SubmitDecisions(BaseModel):
    reviewer: str
    decisions: list[ReviewDecision] = Field(min_length=1)


def create_app(graph=None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.graph = graph or build_graph(analyzer=default_analyzer(), checkpointer=make_checkpointer())
        yield

    app = FastAPI(title="Compliance Review Agent", lifespan=lifespan)

    def _config(thread_id: str, user: str | None = None) -> dict:
        callbacks, metadata = tracing_callbacks(session_id=thread_id, user_id=user)
        return {"configurable": {"thread_id": thread_id}, "callbacks": callbacks, "metadata": metadata}

    def _view(request: Request, thread_id: str) -> dict:
        snapshot = request.app.state.graph.get_state({"configurable": {"thread_id": thread_id}})
        if not snapshot.values:
            raise HTTPException(404, f"No review with thread_id {thread_id}")
        interrupts = [i for task in snapshot.tasks for i in task.interrupts]
        if interrupts:
            return {"thread_id": thread_id, "status": "awaiting_review", "pending": interrupts[0].value}
        if snapshot.values.get("status") == "completed":
            return {"thread_id": thread_id, "status": "completed", "report": snapshot.values["report"]}
        return {"thread_id": thread_id, "status": "running"}

    @app.get("/healthz")
    def healthz():
        return {"ok": True}

    @app.post("/reviews", status_code=201)
    def start_review(body: StartReview, request: Request):
        thread_id = str(uuid.uuid4())
        request.app.state.graph.invoke(body.model_dump(), _config(thread_id))
        return _view(request, thread_id)

    @app.get("/reviews/{thread_id}")
    def get_review(thread_id: str, request: Request):
        return _view(request, thread_id)

    @app.post("/reviews/{thread_id}/decisions")
    def submit_decisions(thread_id: str, body: SubmitDecisions, request: Request):
        current = _view(request, thread_id)
        if current["status"] != "awaiting_review":
            raise HTTPException(409, f"Review is {current['status']}, not awaiting review")
        pending_ids = {f["clause_id"] for f in current["pending"]["findings"]}
        unknown = {d.clause_id for d in body.decisions} - pending_ids
        if unknown:
            raise HTTPException(422, f"Not pending review: {sorted(unknown)}")

        resume = {"reviewer": body.reviewer, "decisions": [d.model_dump() for d in body.decisions]}
        request.app.state.graph.invoke(Command(resume=resume), _config(thread_id, user=body.reviewer))
        return _view(request, thread_id)

    @app.get("/reviews/{thread_id}/history")
    def history(thread_id: str, request: Request):
        cfg = {"configurable": {"thread_id": thread_id}}
        graph = request.app.state.graph
        snapshot = graph.get_state(cfg)
        if not snapshot.values:
            raise HTTPException(404, f"No review with thread_id {thread_id}")
        checkpoints = [
            {"checkpoint_id": s.config["configurable"]["checkpoint_id"], "next": list(s.next), "created_at": s.created_at}
            for s in graph.get_state_history(cfg)
        ]
        return {
            "thread_id": thread_id,
            "audit_log": snapshot.values.get("audit_log", []),
            "metrics": snapshot.values.get("metrics", []),
            "checkpoints": checkpoints,
        }

    return app


app = create_app()
