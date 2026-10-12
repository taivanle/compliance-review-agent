"""Checkpointer and tracing wiring."""

from __future__ import annotations

import atexit
import logging

from app.config import settings

log = logging.getLogger(__name__)

# Only these app types may be rebuilt from a checkpoint (safe deserialisation).
_ALLOWED_TYPES = [("app.state", "Clause"), ("app.state", "Finding")]


def make_serde():
    from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

    return JsonPlusSerializer(allowed_msgpack_modules=_ALLOWED_TYPES)


def make_checkpointer():
    """Postgres when DATABASE_URL is set (local compose, RDS in AWS), else in-memory.

    The Postgres saver stores every step's state, so a paused review survives
    restarts and deploys, and any ECS task can resume any thread.
    """
    if settings.database_url:
        from langgraph.checkpoint.postgres import PostgresSaver
        from psycopg_pool import ConnectionPool

        pool = ConnectionPool(
            settings.database_url,
            max_size=10,
            kwargs={"autocommit": True, "prepare_threshold": 0},
            open=True,
        )
        atexit.register(pool.close)  # drain connections on shutdown (ECS sends SIGTERM)
        saver = PostgresSaver(pool, serde=make_serde())
        saver.setup()  # idempotent: creates checkpoint tables if missing
        log.info("Using Postgres checkpointer")
        return saver

    from langgraph.checkpoint.memory import InMemorySaver

    log.warning("DATABASE_URL not set; using in-memory checkpointer (state is lost on restart)")
    return InMemorySaver(serde=make_serde())


def tracing_callbacks(session_id: str | None = None, user_id: str | None = None) -> tuple[list, dict]:
    """Langfuse callback handler plus trace metadata, or nothing if not configured.

    The handler records every LangGraph node and LLM call as a span, with
    token usage, cost and latency, grouped by session (one per review thread).
    """
    if not settings.langfuse_enabled:
        return [], {}
    from langfuse.langchain import CallbackHandler

    metadata = {}
    if session_id:
        metadata["langfuse_session_id"] = session_id
    if user_id:
        metadata["langfuse_user_id"] = user_id
    return [CallbackHandler()], metadata
