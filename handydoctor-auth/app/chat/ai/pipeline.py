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
import re
from dataclasses import dataclass, field

from app.chat.ai import care
from app.chat.ai.redflags import evaluate as evaluate_red_flags
from app.chat.ai.retrieval import BaseRetriever, Chunk, MockRetriever
from app.chat.ai.triage import classify_query, classify_response

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = """\
You are HandyDoctor, a medical triage assistant grounded in NHS, WHO, and NICE guidelines.

RULES:
1. Base all advice ONLY on the retrieved guideline context provided below.
2. Be clear, concise, and empathetic. Avoid jargon.
3. Always end with ONE next step: self-care at home, ask a pharmacist, contact a GP, get help today (NHS 111 / urgent treatment centre), or call 999.
4. Never diagnose or name a likely condition; describe what to do and which warning signs mean getting help sooner. Always recommend professional review for anything beyond mild self-limiting symptoms.
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

_CRISIS_TERMS = {
    "suicidal", "suicide", "self-harm", "self harm", "harm myself", "hurt myself",
    "want to die", "wanna die", "kill myself", "killing myself", "end my life",
    "end it all", "take my own life", "take my life", "better off dead",
    "no reason to live", "don't want to be alive", "do not want to be alive",
    "don't want to live", "do not want to live", "overdose on purpose",
}


_CRISIS_RE = re.compile(
    r"\b(end(ing)?|take|taking) (my|his|her|their) (own )?li(fe|ves)\b"
    r"|\b(kill|killing|hurt|hurting|harm|harming|cut|cutting) (my|him|her|them)sel(f|ves)\b"
    r"|\bsuicid\w*|\b(don'?t|do not) want to (be alive|live|wake up)"
    r"|\bbetter off (dead|without me)|\bno (point|reason) (in )?(living|to live)"
)


@dataclass
class AIResponse:
    content: str
    severity: str  # 'green' | 'amber' | 'red'  (kept for older clients / emails)
    citations: list[Chunk] = field(default_factory=list)
    is_emergency: bool = False
    care_level: str = "self_care"  # see app/chat/ai/care.py
    reasons: list[dict] = field(default_factory=list)  # why this level (rule hits)


_WHO = {"me": "the user", "child": "the user's child", "other": "someone the user is asking about"}
_DURATION = {
    "today": "started today", "1_3_days": "1 to 3 days", "4_7_days": "4 to 7 days",
    "1_3_weeks": "1 to 3 weeks", "over_3_weeks": "more than 3 weeks",
}


def context_summary(ctx: dict | None) -> str:
    """Plain-English summary of the interview answers, for the prompt and GP summary."""
    if not ctx:
        return ""
    from app.chat.ai.interview import QUESTIONS

    parts = [f"Patient: {_WHO.get(ctx.get('who', 'me'), 'the user')}"]
    if ctx.get("age_years") not in (None, ""):
        parts.append(f"age {ctx['age_years']}")
    if ctx.get("duration"):
        parts.append(f"duration {_DURATION.get(ctx['duration'], ctx['duration'])}")
    if ctx.get("severity"):
        parts.append(f"self-rated severity {ctx['severity']}")
    flags = ctx.get("red_flags") or {}
    yes = [QUESTIONS[q].text for q, v in flags.items() if v is True and q in QUESTIONS]
    no = [QUESTIONS[q].text for q, v in flags.items() if v is False and q in QUESTIONS]
    out = ", ".join(parts) + "."
    if yes:
        out += " Answered YES to: " + " ".join(yes)
    if no:
        out += " Answered NO to: " + " ".join(no)
    return out


# ── Pipeline entry point ──────────────────────────────────────────────────────
async def run_pipeline(
    query: str,
    history: list[dict],  # [{"role": "user"|"assistant", "content": "…"}, …]
    retriever: BaseRetriever | None = None,
    context: dict | None = None,  # guided-interview answers (who/age/duration/red_flags)
) -> AIResponse:
    """
    Main RAG pipeline.  All steps are instrumented with logging so the
    flow is visible in development (LOG_LEVEL=DEBUG).
    """
    retriever = retriever or MockRetriever()

    # ── 1. Emergency pre-check ────────────────────────────────────────────
    lower = query.lower().replace("’", "'")
    if any(t in lower for t in _CRISIS_TERMS) or _CRISIS_RE.search(lower):
        logger.info("pipeline: crisis keywords detected, short-circuiting")
        return AIResponse(
            content=_CRISIS_RESPONSE, severity="red", is_emergency=True, care_level="emergency",
            reasons=[{"id": "crisis", "level": "emergency", "source": "NHS: Help for suicidal thoughts",
                      "reason": "You mentioned thoughts of ending your life or harming yourself."}],
        )

    # Deterministic red-flag rules over everything the user has said in this check.
    earlier = " ".join(m["content"] for m in history if m.get("role") == "user")
    rules = evaluate_red_flags(f"{earlier} {query}", context)
    if (context or {}).get("red_flags", {}).get("mood_safety") is True:
        return AIResponse(
            content=_CRISIS_RESPONSE, severity="red", is_emergency=True, care_level="emergency",
            reasons=rules.reasons,
        )

    if classify_query(query) == "red" or rules.level == "emergency":
        logger.info("pipeline: emergency detected (rules=%s), short-circuiting", rules.level)
        reasons = rules.reasons or [{"id": "keywords", "level": "emergency", "source": "NHS: When to call 999",
                                     "reason": "You described symptoms that can be a medical emergency."}]
        return AIResponse(content=_EMERGENCY_RESPONSE, severity="red", is_emergency=True,
                          care_level="emergency", reasons=reasons)

    # ── 2. Retrieval ──────────────────────────────────────────────────────
    logger.debug("pipeline: retrieving for query=%r", query[:80])
    chunks = await retriever.retrieve(query, top_k=3)
    logger.debug("pipeline: retrieved %d chunks", len(chunks))

    # ── 3. Generate ───────────────────────────────────────────────────────
    content = await _generate(query, chunks, history, context_summary(context))
    content = child_safe(content, context)

    # ── 4. Classify, then apply the rule floor (rules can only raise) ────
    body = content.split("⚕️")[0]  # the standard disclaimer footer mentions GP/111 on every answer
    model_level = care.from_response_text(body, classify_response(body))
    final = care.highest(model_level, rules.level)
    if care.rank(final) > care.rank(model_level):
        lv = care.LEVELS[final]
        why = " ".join(r["reason"] for r in rules.reasons if r["level"] == final)
        content = f"**{lv.label}.** {why} {lv.action}\n\n{content}"
    logger.debug("pipeline: model=%s rules=%s final=%s", model_level, rules.level, final)

    return AIResponse(content=content, severity=care.severity_of(final), citations=chunks,
                      care_level=final, reasons=rules.reasons)


# ── Child safety: no adult doses in answers about children ────────────────
_DOSE = re.compile(r"\d+(\.\d+)?\s*(?:[–-]|to)?\s*\d*\s*(mg|mcg|ml|g/day|grams?)\b", re.I)
_CHILD_NOTE = (
    "Medicine doses for children depend on their age and weight. Check the packet or ask a "
    "pharmacist before giving anything, and never give aspirin to under-16s."
)


def child_safe(content: str, context: dict | None) -> str:
    """Remove adult-dose sentences when the check is about a child (under 16)."""
    if not context:
        return content
    age = context.get("age_years")
    is_child = context.get("who") == "child" or (isinstance(age, (int, float)) and age < 16)
    if not is_child or not _DOSE.search(content):
        return content
    kept = []
    for para in content.split("\n"):
        sentences = re.split(r"(?<=[.!?])\s+", para)
        kept.append(" ".join(x for x in sentences if not _DOSE.search(x)))
    out = "\n".join(kept).replace("\n\n\n", "\n\n")
    main, sep, footer = out.partition("⚕️")
    return f"{main.rstrip()}\n\n{_CHILD_NOTE}\n\n{sep}{footer}" if sep else f"{out}\n\n{_CHILD_NOTE}"


# ── Generation ────────────────────────────────────────────────────────────────
async def _generate(query: str, chunks: list[Chunk], history: list[dict], ctx_text: str = "") -> str:
    """Try OpenAI; fall back to MockGenerator if key absent or on error."""
    from app.config import settings

    api_key = getattr(settings, "openai_api_key", "")
    if api_key:
        try:
            q = f"{query}\n\n[Guided interview] {ctx_text}" if ctx_text else query
            return await _openai_generate(
                q, chunks, history, api_key, getattr(settings, "openai_model", "gpt-4o-mini")
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
