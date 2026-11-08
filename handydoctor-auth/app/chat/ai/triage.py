"""
app/chat/ai/triage.py — Symptom severity classifier.

Two-phase approach
──────────────────
1. Keyword pre-check on the raw user query — catches obvious emergencies
   before any LLM call (fast, no network cost).

2. Content analysis of the generated response — looks for language that
   the LLM or the retrieved guidelines use to indicate severity.  This
   ensures the badge reflects the *response*, not just the query.

Returns one of: 'green' | 'amber' | 'red'
"""

from __future__ import annotations

import re

# ── Emergency terms (trigger 'red' immediately on query) ─────────────────────
_EMERGENCY_TERMS: list[str] = [
    "chest pain",
    "can't breathe",
    "cannot breathe",
    "difficulty breathing",
    "not breathing",
    "stopped breathing",
    "unconscious",
    "unresponsive",
    "stroke",
    "heart attack",
    "severe bleeding",
    "overdose",
    "poisoning",
    "anaphylaxis",
    "throat swelling",
    "can't swallow",
    "sepsis",
    "seizure",
    "fitting",
    "collapse",
    "collapsed",
    "suicidal",
    "want to die",
    "kill myself",
    "end my life",
    "take my own life",
    "suicide",
    "hurt myself",
]

# ── Response content indicators ───────────────────────────────────────────────
_RED_PATTERNS: list[str] = [
    r"call 999",
    r"go to a&e",
    r"go to the a&e",
    r"emergency",
    r"immediately",
    r"life.threatening",
    r"do not wait",
    r"ambulance",
    r"urgent.care.*immediately",
]

_AMBER_PATTERNS: list[str] = [
    r"see a gp",
    r"visit a gp",
    r"book.*gp",
    r"gp.*appointment",
    r"urgent care centre",
    r"walk.in centre",
    r"(gp|doctor|seen|appointment|contact).{0,40}within \d+ ?(hours|days)",
    r"same.day",
    r"soon as possible",
    r"nhs 111",
    r"seek.*medical",
    r"medical.*attention",
]


def classify_query(query: str) -> str | None:
    """
    Return 'red' if the query contains emergency terms, else None.
    Called *before* the LLM to fast-path emergencies.
    """
    lower = query.lower()
    if any(term in lower for term in _EMERGENCY_TERMS):
        return "red"
    return None


_CONDITIONAL = re.compile(
    r"\b(if|unless|in case|should you|when to|whenever|any of these|warning signs?"
    r"|may (indicate|be a sign|suggest)|can (indicate|be a sign)|signs? of)\b"
)


def _directive_sentences(text: str) -> list[str]:
    """
    Sentences that tell the user what to do NOW.  Safety-netting such as
    "call 999 if you develop a rash" appears in almost every answer and must
    not, on its own, turn a cold into an emergency.
    """
    parts = re.split(r"(?<=[.!?])\s+|\n+", text)
    return [p for p in parts if p.strip() and not _CONDITIONAL.search(p)]


def classify_response(content: str) -> str:
    """
    Derive a severity from the generated response text, ignoring conditional
    safety-netting sentences. Falls back to 'green' (self-care).
    """
    lower = " ".join(_directive_sentences(content.lower()))
    if any(re.search(p, lower) for p in _RED_PATTERNS):
        return "red"
    if any(re.search(p, lower) for p in _AMBER_PATTERNS):
        return "amber"
    return "green"


def severity_badge(severity: str) -> str:
    return {
        "green": "✅ Self-care at home",
        "amber": "⚠️ See a GP soon",
        "red": "🚨 Call 999 / A&E",
    }.get(severity, "✅ Self-care at home")
