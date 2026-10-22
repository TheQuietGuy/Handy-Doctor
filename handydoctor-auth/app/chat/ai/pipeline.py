"""
app/chat/ai/pipeline.py — RAG pipeline orchestrator.

Flow
────
User query
  ↓
1. Emergency pre-check (triage.classify_query)
   → if red: return canned emergency response immediately (no LLM call)

2. Retrieval  (retriever.retrieve)
   → top-k relevant NHS/WHO/NICE guideline chunks

3. Prompt construction
   → system prompt (role + safety disclaimer)
   → context block (retrieved chunks)
   → conversation history (last N turns)
   → user message

4. Generation (OpenAI GPT-4o if key set, otherwise MockGenerator)

5. Triage classification (triage.classify_response)
   → derive severity badge from generated content

6. Return AIResponse
   → content, severity, citations, is_emergency flag

Config
──────
Set OPENAI_API_KEY in .env to enable real generation.
Set OPENAI_MODEL   to override (default: gpt-4o-mini for cost efficiency).
Leave key unset → MockGenerator (deterministic, no external calls).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from app.chat.ai.retrieval import BaseRetriever, Chunk, MockRetriever
from app.chat.ai.triage import classify_query, classify_response

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = """\
You are HandyDoctor, a medical triage assistant grounded in NHS, WHO, and NICE guidelines.

RULES:
1. Base all advice ONLY on the retrieved guideline context provided below.
2. Be clear, concise, and empathetic. Avoid jargon.
3. Always include a triage recommendation: self-care, see a GP, or call 999.
4. Never diagnose. Always recommend professional review for anything beyond mild self-limiting symptoms.
5. End every response with: "⚕️ This is not a substitute for professional medical advice."

RETRIEVED GUIDELINE CONTEXT:
{context}
"""

_EMERGENCY_RESPONSE = (
    "🚨 Based on the symptoms you have described, this may be a medical emergency. "
    "Please call 999 or go to your nearest Accident & Emergency department immediately. "
    "Do not drive yourself — ask someone to take you or wait for an ambulance.\n\n"
    "If you are unsure whether this is an emergency, call NHS 111 (free, 24/7).\n\n"
    "⚕️ This is not a substitute for professional medical advice."
)

_CRISIS_RESPONSE = (
    "I'm very concerned about what you've shared. If you're having thoughts of "
    "self-harm or suicide, please reach out right now:\n\n"
    "📞 **Samaritans** — call 116 123 (free, 24/7)\n"
    "💬 **SHOUT** — text SHOUT to 85258\n"
    "🏥 If you are in immediate danger, call 999.\n\n"
    "You are not alone. Help is available right now.\n\n"
    "⚕️ This is not a substitute for professional medical advice."
)

_CRISIS_TERMS = {"suicidal", "self-harm", "want to die", "kill myself"}


@dataclass
class AIResponse:
    content: str
    severity: str  # 'green' | 'amber' | 'red'
    citations: list[Chunk] = field(default_factory=list)
    is_emergency: bool = False


# ── Pipeline entry point ──────────────────────────────────────────────────────
async def run_pipeline(
    query: str,
    history: list[dict],  # [{"role": "user"|"assistant", "content": "…"}, …]
    retriever: BaseRetriever | None = None,
) -> AIResponse:
    """
    Main RAG pipeline.  All steps are instrumented with logging so the
    flow is visible in development (LOG_LEVEL=DEBUG).
    """
    retriever = retriever or MockRetriever()

    # ── 1. Emergency pre-check ────────────────────────────────────────────
    lower = query.lower()
    if any(t in lower for t in _CRISIS_TERMS):
        logger.info("pipeline: crisis keywords detected, short-circuiting")
        return AIResponse(content=_CRISIS_RESPONSE, severity="red", is_emergency=True)

    emergency_severity = classify_query(query)
    if emergency_severity == "red":
        logger.info("pipeline: emergency keywords detected, short-circuiting")
        return AIResponse(content=_EMERGENCY_RESPONSE, severity="red", is_emergency=True)

    # ── 2. Retrieval ──────────────────────────────────────────────────────
    logger.debug("pipeline: retrieving for query=%r", query[:80])
    chunks = await retriever.retrieve(query, top_k=3)
    logger.debug("pipeline: retrieved %d chunks", len(chunks))

    # ── 3. Generate ───────────────────────────────────────────────────────
    content = await _generate(query, chunks, history)

    # ── 4. Classify severity ──────────────────────────────────────────────
    severity = classify_response(content)
    logger.debug("pipeline: severity=%s", severity)

    return AIResponse(content=content, severity=severity, citations=chunks)


# ── Generation ────────────────────────────────────────────────────────────────
async def _generate(query: str, chunks: list[Chunk], history: list[dict]) -> str:
    """Try OpenAI; fall back to MockGenerator if key absent or on error."""
    from app.config import settings

    api_key = getattr(settings, "openai_api_key", "")
    if api_key:
        try:
            return await _openai_generate(
                query, chunks, history, api_key, getattr(settings, "openai_model", "gpt-4o-mini")
            )
        except Exception:
            logger.exception("pipeline: OpenAI call failed, falling back to mock")

    return _mock_generate(query, chunks)


async def _openai_generate(
    query: str,
    chunks: list[Chunk],
    history: list[dict],
    api_key: str,
    model: str,
) -> str:
    try:
        from openai import AsyncOpenAI
    except ImportError as err:
        raise RuntimeError("pip install openai to enable real generation") from err

    client = AsyncOpenAI(api_key=api_key)
    context = "\n\n".join(f"[{c.source}]\n{c.content}" for c in chunks)
    system = _SYSTEM_PROMPT.format(context=context)

    # Keep last 10 turns to stay within context limits
    trimmed_history = history[-10:]
    messages = [{"role": "system", "content": system}]
    messages += trimmed_history
    messages.append({"role": "user", "content": query})

    resp = await client.chat.completions.create(
        model=model,
        messages=messages,
        max_tokens=600,
        temperature=0.2,  # low temperature for factual medical content
    )
    return resp.choices[0].message.content.strip()


def _mock_generate(query: str, chunks: list[Chunk]) -> str:
    """
    Deterministic mock generator — uses the retrieved chunks to build
    a plausible, specific response without any external API call.
    """
    if not chunks:
        return (
            "Based on your description, I was unable to locate specific guidelines "
            "that match your query. Please describe your symptoms in more detail, "
            "or consider contacting NHS 111 for personalised guidance.\n\n"
            "⚕️ This is not a substitute for professional medical advice."
        )

    primary = chunks[0]

    # Build a response that incorporates the actual retrieved content
    intro_map = {
        "green": "Based on the NHS and NICE guidelines relevant to your symptoms, "
        "this may be something you can manage safely at home for now.",
        "amber": "Your symptoms warrant attention from a healthcare professional. "
        "Based on the relevant NHS guidelines:",
        "red": "The guidelines indicate this requires prompt medical assessment.",
    }
    severity_guess = classify_response(primary.content)
    intro = intro_map.get(severity_guess, intro_map["green"])

    # Trim the chunk content to a readable length
    chunk_excerpt = primary.content[:500]
    if len(primary.content) > 500:
        chunk_excerpt += "…"

    response = (
        f"{intro}\n\n"
        f"{chunk_excerpt}\n\n"
        f"⚕️ This is not a substitute for professional medical advice. "
        f"If your symptoms worsen or you are concerned at any point, "
        f"contact your GP or call NHS 111."
    )
    return response
