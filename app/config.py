"""Runtime configuration, read from environment variables.

In AWS these are injected into the ECS task from Secrets Manager / task env,
so nothing sensitive lives in the image or the repo.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field


def _float(name: str, default: float) -> float:
    return float(os.getenv(name, default))


@dataclass(frozen=True)
class Settings:
    # Which analyzer to use:
    #   rules     - deterministic baseline, no model (default; used by unit tests)
    #   ollama    - local open-weights model via Ollama. Free: runs on your machine.
    llm_provider: str = field(default_factory=lambda: os.getenv("LLM_PROVIDER", "rules").lower())

    ollama_model: str = field(default_factory=lambda: os.getenv("OLLAMA_MODEL", "qwen2.5:7b"))
    ollama_base_url: str = field(default_factory=lambda: os.getenv("OLLAMA_BASE_URL", "http://localhost:11434"))

    # USD per million tokens, used to put a cost on every call. 0 for a local
    # model. Set these to a hosted API's list price to report what the same workload
    # would cost on that API (a useful "self-hosting saved $X" number).
    input_cost_per_mtok: float = field(default_factory=lambda: _float("INPUT_COST_PER_MTOK", 0.0))
    output_cost_per_mtok: float = field(default_factory=lambda: _float("OUTPUT_COST_PER_MTOK", 0.0))

    # Checkpointing. Postgres in every real environment; in-memory for tests.
    database_url: str | None = field(default_factory=lambda: os.getenv("DATABASE_URL"))

    # Langfuse tracing is switched on when both keys are present.
    langfuse_public_key: str | None = field(default_factory=lambda: os.getenv("LANGFUSE_PUBLIC_KEY"))
    langfuse_secret_key: str | None = field(default_factory=lambda: os.getenv("LANGFUSE_SECRET_KEY"))

    # Findings at or above this risk level pause the graph for a human.
    review_threshold: str = field(default_factory=lambda: os.getenv("REVIEW_THRESHOLD", "high"))

    @property
    def langfuse_enabled(self) -> bool:
        return bool(self.langfuse_public_key and self.langfuse_secret_key)


settings = Settings()
