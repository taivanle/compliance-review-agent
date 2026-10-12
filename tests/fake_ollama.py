"""A tiny stand-in for the Ollama HTTP API.

Lets the test suite exercise the real ChatOllama client, structured-output
parsing and token accounting in CI without downloading a model. Answers are
produced by the rule-based analyzer, returned in Ollama's /api/chat format.
"""

from __future__ import annotations

import json
import re
import socket
import threading
import time

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse

from app.analyzers import RuleBasedAnalyzer
from app.state import Clause

_rules = RuleBasedAnalyzer()


def _answer(prompt: str, schema: dict) -> dict:
    props = (schema or {}).get("properties", {})
    if "clauses" in props:
        document = prompt.split("Document:\n", 1)[1]
        clauses, _ = _rules.extract_clauses(document)
        return {"clauses": [c.model_dump() for c in clauses]}
    if "findings" in props:
        clauses = [
            Clause(id=m[0], category=m[1], text=m[2])
            for m in re.findall(r"^\[(c\d+)\] \((\w+)\) (.*)$", prompt, flags=re.M)
        ]
        findings, _ = _rules.score_risk(clauses)
        return {"findings": [f.model_dump(include={"clause_id", "category", "risk", "rationale"}) for f in findings]}
    return {"remediation": "Narrow the clause and add a notice period."}


def make_app() -> FastAPI:
    app = FastAPI()
    app.state.calls = 0

    @app.post("/api/chat")
    async def chat(request: Request):
        body = await request.json()
        app.state.calls += 1
        prompt = body["messages"][-1]["content"]
        content = json.dumps(_answer(prompt, body.get("format")))
        final = {
            "model": body["model"],
            "created_at": "2026-01-01T00:00:00Z",
            "message": {"role": "assistant", "content": content},
            "done": True,
            "done_reason": "stop",
            "prompt_eval_count": len(prompt) // 4,
            "eval_count": len(content) // 4,
        }
        if body.get("stream", True):
            return StreamingResponse(iter([json.dumps(final) + "\n"]), media_type="application/x-ndjson")
        return final

    return app


class FakeOllama:
    """Context manager that runs the fake server on a free port."""

    def __enter__(self):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            self.port = s.getsockname()[1]
        self.app = make_app()
        self.server = uvicorn.Server(uvicorn.Config(self.app, port=self.port, log_level="warning"))
        self.thread = threading.Thread(target=self.server.run, daemon=True)
        self.thread.start()
        while not self.server.started:
            time.sleep(0.02)
        self.url = f"http://127.0.0.1:{self.port}"
        return self

    def __exit__(self, *exc):
        self.server.should_exit = True
        self.thread.join(timeout=5)
