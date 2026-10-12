"""The two things the agent asks a model to do: extract clauses and score risk.

`LLMAnalyzer` uses a local open-weights model (via Ollama) with structured output. `RuleBasedAnalyzer` is a
deterministic stand-in with the same interface, used when no API key is set:
it keeps unit tests and CI offline, and gives the eval harness a baseline to
compare the LLM against.

Every call returns a `Usage` so nodes can record tokens and cost per step.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Protocol

from pydantic import BaseModel

from app.config import Settings, settings
from app.state import Clause, Finding, RiskLevel


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0


class Analyzer(Protocol):
    name: str

    def extract_clauses(self, document: str) -> tuple[list[Clause], Usage]: ...

    def score_risk(self, clauses: list[Clause]) -> tuple[list[Finding], Usage]: ...

    def draft_remediation(self, finding: Finding, clause: Clause) -> tuple[str, Usage]: ...


# --------------------------------------------------------------------------
# Rule-based baseline
# --------------------------------------------------------------------------

_CATEGORY_KEYWORDS: dict[str, tuple[str, ...]] = {
    "liability": ("liability", "liable", "indemnif", "damages"),
    "data_protection": ("personal data", "gdpr", "data protection", "data subject", "processor"),
    "termination": ("terminat", "notice period", "renew"),
    "payment": ("payment", "invoice", "fee", "price"),
    "confidentiality": ("confidential", "non-disclosure"),
    "governing_law": ("governing law", "jurisdiction", "courts of"),
}

# (pattern, risk, rationale). First match wins, so order matters.
_RISK_RULES: list[tuple[str, RiskLevel, str]] = [
    (r"unlimited liability|liability shall not be (?:limited|capped)", "high",
     "Uncapped liability exposure."),
    (r"outside (?:the )?(?:uk|united kingdom|eea)(?!.*(?:adequacy|standard contractual|safeguard))", "high",
     "International data transfer with no stated safeguard."),
    (r"without (?:prior )?notice", "high",
     "Counterparty can act without notice."),
    (r"automatically renew", "medium",
     "Auto-renewal; check the cancellation window."),
    (r"sole (?:and absolute )?discretion", "medium",
     "One-sided discretion for the counterparty."),
    (r"(?:within|after) (?:9[0-9]|1[0-9]{2}) days", "medium",
     "Long payment or notice term."),
]


class RuleBasedAnalyzer:
    name = "rules"

    def extract_clauses(self, document: str) -> tuple[list[Clause], Usage]:
        # Split on blank lines or numbered headings like "1." / "4.2".
        parts = [p.strip() for p in re.split(r"\n\s*\n|\n(?=\d+(?:\.\d+)*\.?\s)", document) if p.strip()]
        clauses = [
            Clause(id=f"c{i + 1}", text=part, category=self._categorise(part))
            for i, part in enumerate(parts)
        ]
        return clauses, Usage()

    def score_risk(self, clauses: list[Clause]) -> tuple[list[Finding], Usage]:
        findings = []
        for clause in clauses:
            risk, rationale = self._score(clause.text)
            findings.append(Finding(clause_id=clause.id, category=clause.category, risk=risk, rationale=rationale))
        return findings, Usage()

    def draft_remediation(self, finding: Finding, clause: Clause) -> tuple[str, Usage]:
        templates = {
            "liability": "Cap liability at fees paid in the preceding 12 months, excluding fraud and death or personal injury.",
            "data_protection": "Require UK IDTA or SCCs plus a transfer risk assessment before any restricted transfer.",
            "termination": "Require at least 30 days' written notice and a cure period for remediable breaches.",
            "payment": "Move to 30-day payment terms with late-payment interest under the Late Payment Act.",
        }
        text = templates.get(finding.category, "Escalate to legal for redrafting.")
        return text, Usage()

    @staticmethod
    def _categorise(text: str) -> str:
        lowered = text.lower()
        for category, keywords in _CATEGORY_KEYWORDS.items():
            if any(k in lowered for k in keywords):
                return category
        return "other"

    @staticmethod
    def _score(text: str) -> tuple[RiskLevel, str]:
        lowered = " ".join(text.lower().split())
        for pattern, risk, rationale in _RISK_RULES:
            if re.search(pattern, lowered):
                return risk, rationale
        return "low", "No risk pattern detected."


# --------------------------------------------------------------------------
# LLM analyzer (Ollama, structured output)
# --------------------------------------------------------------------------

class _ClauseList(BaseModel):
    clauses: list[Clause]


class _FindingList(BaseModel):
    findings: list[Finding]


class _Remediation(BaseModel):
    remediation: str


_EXTRACT_PROMPT = """You are reviewing a commercial contract for a UK financial services firm.
Split the document into its individual clauses. Give each an id (c1, c2, ...),
its exact text, and one category from: liability, data_protection, termination,
payment, confidentiality, governing_law, other.

Document:
{document}"""

_SCORE_PROMPT = """For each clause below, assign a risk level for a UK regulated firm:
- high: uncapped liability, restricted data transfers with no safeguard, unilateral action without notice
- medium: one-sided discretion, auto-renewal, long payment/notice terms
- low: standard wording
Give a one-sentence rationale. Return one finding per clause, keeping clause_id and category.

Clauses:
{clauses}"""

_REMEDIATE_PROMPT = """Draft a one-or-two sentence remediation for this contract clause,
suitable for a negotiation markup. Reviewer note (may be empty): {note}

Clause ({category}, {risk} risk): {text}"""


class LLMAnalyzer:
    """Any LangChain chat model with structured output.

    Ollama (local, free) is the backend. Any other LangChain chat model can be
    passed in as `chat_model`; same prompts and schemas, so evals compare directly.
    """

    def __init__(self, cfg: Settings = settings, callbacks: list | None = None, chat_model=None):
        self._cfg = cfg
        self._callbacks = callbacks or []
        if chat_model is not None:
            self._llm, self.name = chat_model, "custom"
        elif cfg.llm_provider == "ollama":
            from langchain_ollama import ChatOllama

            self._llm = ChatOllama(model=cfg.ollama_model, base_url=cfg.ollama_base_url, temperature=0)
            self.name = f"ollama:{cfg.ollama_model}"
        else:
            raise ValueError(f"Unknown LLM_PROVIDER {cfg.llm_provider!r}")

    def _call(self, schema: type[BaseModel], prompt: str):
        structured = self._llm.with_structured_output(schema, include_raw=True)
        out = structured.invoke(prompt, config={"callbacks": self._callbacks})
        meta = getattr(out["raw"], "usage_metadata", None) or {}
        usage = Usage(input_tokens=meta.get("input_tokens", 0), output_tokens=meta.get("output_tokens", 0))
        usage.cost_usd = (
            usage.input_tokens * self._cfg.input_cost_per_mtok
            + usage.output_tokens * self._cfg.output_cost_per_mtok
        ) / 1_000_000
        if out.get("parsing_error"):
            raise ValueError(f"Structured output failed to parse: {out['parsing_error']}")
        return out["parsed"], usage

    def extract_clauses(self, document: str) -> tuple[list[Clause], Usage]:
        parsed, usage = self._call(_ClauseList, _EXTRACT_PROMPT.format(document=document))
        return parsed.clauses, usage

    def score_risk(self, clauses: list[Clause]) -> tuple[list[Finding], Usage]:
        listing = "\n".join(f"[{c.id}] ({c.category}) {c.text}" for c in clauses)
        parsed, usage = self._call(_FindingList, _SCORE_PROMPT.format(clauses=listing))
        return parsed.findings, usage

    def draft_remediation(self, finding: Finding, clause: Clause) -> tuple[str, Usage]:
        prompt = _REMEDIATE_PROMPT.format(
            note=finding.reviewer_note or "", category=finding.category, risk=finding.risk, text=clause.text
        )
        parsed, usage = self._call(_Remediation, prompt)
        return parsed.remediation, usage


def default_analyzer(callbacks: list | None = None) -> Analyzer:
    return RuleBasedAnalyzer() if settings.llm_provider == "rules" else LLMAnalyzer(callbacks=callbacks)
