"""
app/chat/ai.py — AI response generation for HandyDoctor chat.

Pipeline for each user message
────────────────────────────────
1.  Emergency pre-flight check (local, no API call)
    If the message contains emergency keywords we return a hardcoded
    999 response immediately — fast, reliable, never fails.

2.  Anthropic API call (claude-haiku-4-5-20251001 by default)
    System prompt grounds the model in NHS / NICE / WHO guidance and
    requires a structured JSON response.

3.  Parse and validate the JSON
    Falls back to a safe generic response if parsing fails.

4.  Auto-derive session title from the first user message
    The first turn is summarised into ≤7 words for the session title.

Structured response contract
─────────────────────────────
The model is instructed to respond ONLY with a JSON object:
{
  "response":     "<plain English guidance, 150-300 words>",
  "severity":     "self_care | see_gp | urgent_care | emergency",
  "triage_badge": "✅ Self-care at home | ⚠️ See a GP soon | 🏥 Go to urgent care | 🚨 Call 999",
  "citations":    ["<source>", ...],   // 1-3 items
  "is_emergency": true | false
}
"""

import json
import logging
import re
from dataclasses import dataclass

import anthropic

from app.config import settings

logger = logging.getLogger(__name__)

# ── Emergency keyword lists ───────────────────────────────────────────────────
_EMERGENCY_KW = {
    "chest pain",
    "heart attack",
    "cardiac arrest",
    "can't breathe",
    "cannot breathe",
    "difficulty breathing",
    "not breathing",
    "stroke",
    "face drooping",
    "arm weakness",
    "speech slurred",
    "severe bleeding",
    "uncontrolled bleeding",
    "unconscious",
    "unresponsive",
    "passed out",
    "overdose",
    "poisoning",
    "anaphylaxis",
    "severe allergic reaction",
    "sepsis",
    "blood poisoning",
    "seizure",
    "convulsion",
    "fitting",
    "collapse",
    "collapsed",
    "choking",
    "stabbing",
    "gunshot",
    "suicidal",
    "self-harm",
    "want to die",
    "kill myself",
}

_MENTAL_HEALTH_KW = {"suicidal", "self-harm", "want to die", "kill myself"}

# ── Triage constants ──────────────────────────────────────────────────────────
_BADGE = {
    "self_care": "✅ Self-care at home",
    "see_gp": "⚠️ See a GP soon",
    "urgent_care": "🏥 Go to urgent care",
    "emergency": "🚨 Call 999 immediately",
}

_VALID_SEVERITIES = set(_BADGE.keys())

# ── System prompt ─────────────────────────────────────────────────────────────
_SYSTEM_PROMPT = """You are HandyDoctor, a medical triage assistant that provides \
evidence-based health guidance grounded exclusively in NHS, NICE CKS, and WHO guidelines.

CRITICAL RULES:
- You are NOT a doctor. You triage and advise; you never diagnose.
- Always ground advice in real NHS/NICE/WHO guidance.
- If symptoms suggest a life-threatening emergency, set severity to "emergency" \
and is_emergency to true.
- End every response with the disclaimer: \
"⚕️ This is not a substitute for professional medical advice."
- Be empathetic, clear, and concise (150-300 words in the "response" field).

OUTPUT FORMAT — respond ONLY with a valid JSON object, no markdown fences, \
no preamble, no trailing text:
{
  "response": "<your guidance in plain English>",
  "severity": "self_care | see_gp | urgent_care | emergency",
  "triage_badge": "✅ Self-care at home | ⚠️ See a GP soon | 🏥 Go to urgent care | 🚨 Call 999 immediately",
  "citations": ["<source 1>", "<source 2>"],
  "is_emergency": true | false
}

Severity decision rules:
- self_care   : minor symptoms manageable without clinical contact
- see_gp      : symptoms warrant GP assessment within 1-2 days
- urgent_care : symptoms need same-day clinical assessment (not 999)
- emergency   : life-threatening — call 999 or go to A&E immediately"""


@dataclass
class AIResponse:
    content: str
    severity: str
    triage_badge: str
    citations: list[str]
    is_emergency: bool


# ── Public interface ──────────────────────────────────────────────────────────
def detect_emergency(text: str) -> tuple[bool, bool]:
    """
    Returns (is_emergency, is_mental_health).
    Called before the API to short-circuit on obvious emergencies.
    """
    lower = text.lower()
    is_mental = any(kw in lower for kw in _MENTAL_HEALTH_KW)
    is_general = any(kw in lower for kw in _EMERGENCY_KW)
    return is_general, is_mental


async def generate_response(
    user_message: str,
    history: list[dict],  # [{"role": "user"|"assistant", "content": str}, ...]
) -> AIResponse:
    """
    Generate an AI triage response for the given user message.
    Returns a structured AIResponse.  Never raises — falls back to a
    safe generic response on any error.
    """
    is_emergency, is_mental = detect_emergency(user_message)

    if is_emergency:
        return _emergency_response(mental_health=is_mental)

    if not settings.anthropic_api_key:
        return _fallback_response()

    try:
        return await _call_anthropic(user_message, history)
    except Exception:
        logger.exception("Anthropic API call failed; using fallback response")
        return _fallback_response()


async def derive_session_title(first_user_message: str) -> str:
    """
    Summarise the first message into ≤7 words for the session title.
    Falls back to a truncated version of the message if the API is unavailable.
    """
    if not settings.anthropic_api_key:
        return _truncate_title(first_user_message)

    try:
        client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
        msg = await client.messages.create(
            model=settings.anthropic_model,
            max_tokens=30,
            messages=[
                {
                    "role": "user",
                    "content": (
                        f"Summarise this patient message in 4-6 words as a consultation title. "
                        f"Reply with ONLY the title, no punctuation at the end:\n\n{first_user_message[:300]}"
                    ),
                }
            ],
        )
        title = msg.content[0].text.strip().rstrip(".")
        return title[:200] if title else _truncate_title(first_user_message)
    except Exception:
        return _truncate_title(first_user_message)


# ── Internal ──────────────────────────────────────────────────────────────────
async def _call_anthropic(user_message: str, history: list[dict]) -> AIResponse:
    client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)

    # Build message list: history (up to last 10 turns) + current message
    messages = history[-10:] + [{"role": "user", "content": user_message}]

    response = await client.messages.create(
        model=settings.anthropic_model,
        max_tokens=settings.chat_max_tokens,
        system=_SYSTEM_PROMPT,
        messages=messages,
    )

    raw = response.content[0].text.strip()
    return _parse_response(raw)


def _parse_response(raw: str) -> AIResponse:
    """Parse the structured JSON the model is instructed to return."""
    # Strip any accidental markdown fences
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.MULTILINE).strip()

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("AI returned non-JSON; raw=%r", raw[:200])
        return _fallback_response(raw)

    severity = data.get("severity", "see_gp")
    if severity not in _VALID_SEVERITIES:
        severity = "see_gp"

    return AIResponse(
        content=data.get("response", raw),
        severity=severity,
        triage_badge=data.get("triage_badge", _BADGE[severity]),
        citations=data.get("citations", [])[:3],
        is_emergency=bool(data.get("is_emergency", False)),
    )


def _emergency_response(mental_health: bool = False) -> AIResponse:
    if mental_health:
        return AIResponse(
            content=(
                "I'm concerned about what you've shared. If you're having thoughts "
                "of self-harm or suicide, please reach out for support immediately.\n\n"
                "📞 Samaritans: 116 123 (free, 24/7)\n"
                "📱 Text SHOUT to 85258\n"
                "🏥 Or go to your nearest A&E — you don't have to face this alone.\n\n"
                "⚕️ This is not a substitute for professional medical advice."
            ),
            severity="emergency",
            triage_badge="🚨 Call 999 immediately",
            citations=["📄 Samaritans: samaritans.org", "📄 NHS: Mental health crisis"],
            is_emergency=True,
        )
    return AIResponse(
        content=(
            "🚨 Based on the symptoms you've described, this may be a medical emergency. "
            "Please call 999 or go to your nearest A&E immediately. "
            "Do not drive yourself — call for help now.\n\n"
            "While waiting for help:\n"
            "• Stay calm and stay on the line with the 999 operator\n"
            "• Do not eat or drink anything\n"
            "• Unlock your front door if possible\n\n"
            "⚕️ This is not a substitute for professional medical advice."
        ),
        severity="emergency",
        triage_badge="🚨 Call 999 immediately",
        citations=["📄 NHS: When to call 999", "📄 NHS 111 Online"],
        is_emergency=True,
    )


def _fallback_response(raw_content: str = "") -> AIResponse:
    """Used when the API is unavailable or returns unparseable output."""
    content = raw_content or (
        "I wasn't able to process your message fully at this time. "
        "If your symptoms are urgent or you're worried, please contact NHS 111 "
        "or visit your GP. For emergencies, always call 999.\n\n"
        "⚕️ This is not a substitute for professional medical advice."
    )
    return AIResponse(
        content=content,
        severity="see_gp",
        triage_badge="⚠️ See a GP soon",
        citations=["📄 NHS 111: 111.nhs.uk"],
        is_emergency=False,
    )


def _truncate_title(text: str) -> str:
    words = text.split()
    return " ".join(words[:6]) + ("…" if len(words) > 6 else "")
