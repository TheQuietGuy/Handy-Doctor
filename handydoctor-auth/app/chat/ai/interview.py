"""
app/chat/ai/interview.py - Short guided interview before the first answer.

An Oxford randomised trial (2025, n=1,298) found people using chatbots for
health decisions did no better than search, mostly because they didn't
know what information to give.  Professional checkers (NHS 111 online,
Infermedica) ask targeted questions first.  This module picks up to five
yes/no red-flag questions based on the complaint, plus the standard
who / age / duration / severity questions the frontend always asks.

It is deterministic: the same complaint always produces the same questions.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.chat.ai.redflags import evaluate


@dataclass(frozen=True)
class Question:
    id: str
    text: str
    level_if_yes: str
    reason: str
    source: str


def _q(id, text, level, reason, source):
    return id, Question(id, text, level, reason, source)


QUESTIONS: dict[str, Question] = dict([
    _q("chest_spread", "Does the pain spread to your arm, jaw, neck or back?", "emergency",
       "Chest pain spreading to the arm, jaw, neck or back can be a heart attack.", "NHS: Heart attack"),
    _q("sweaty_breathless", "Are you sweating, feeling sick or short of breath with it?", "emergency",
       "Chest pain with sweating, nausea or breathlessness needs a 999 call.", "NHS: Heart attack"),
    _q("breath_rest", "Are you short of breath even when resting?", "urgent",
       "Breathlessness at rest needs assessment today.", "NHS: Shortness of breath"),
    _q("blue_lips", "Are your lips or skin turning blue or grey?", "emergency",
       "Blue or grey lips are a sign of low oxygen.", "NHS: Shortness of breath"),
    _q("sudden_severe", "Did it come on suddenly and is it the worst you've ever had?", "emergency",
       "A sudden, severe headache needs emergency assessment.", "NHS: Headaches"),
    _q("stiff_neck", "Do you have a stiff neck, or does light hurt your eyes?", "emergency",
       "A stiff neck or sensitivity to light with headache or fever can be meningitis.", "NHS: Meningitis"),
    _q("fast_signs", "Any face drooping, arm weakness, or slurred or confused speech?", "emergency",
       "These are signs of a stroke (FAST).", "NHS: Stroke"),
    _q("head_injury", "Did it start after a blow to the head?", "urgent",
       "Symptoms after a head injury should be checked today.", "NHS: Head injury"),
    _q("rash_glass", "Is there a rash that doesn't fade when you press a glass on it?", "emergency",
       "A rash that doesn't fade under a glass can be sepsis or meningitis.", "NHS: Meningitis"),
    _q("fever", "Do you have a high temperature (38°C or above), or feel hot and shivery?", "self_care",
       "", "NHS: High temperature"),
    _q("very_unwell", "Do you feel very unwell, confused, or much worse than with a normal bug?", "urgent",
       "Feeling very unwell or confused with an infection can be an early sign of sepsis.", "NHS: Sepsis"),
    _q("lower_right", "Has the pain moved to, or settled in, the lower right of your tummy?", "urgent",
       "Pain in the lower right of the tummy can be appendicitis.", "NHS: Appendicitis"),
    _q("cant_keep_fluids", "Have you been unable to keep fluids down for more than a day?", "urgent",
       "Not keeping fluids down risks dehydration.", "NHS: Diarrhoea and vomiting"),
    _q("blood_vomit_poo", "Is there blood in your vomit or poo, or is your poo black?", "urgent",
       "Blood in vomit or poo needs checking today.", "NHS: Vomiting blood"),
    _q("pregnant", "Could you be pregnant?", "gp",
       "Some symptoms and medicines need extra care in pregnancy.", "NHS: Pregnancy"),
    _q("back_pain_fever", "Do you also have pain in your back or side, or a temperature?", "urgent",
       "Urinary symptoms with back pain or a temperature can be a kidney infection.", "NHS: Kidney infection"),
    _q("blood_pee", "Is there blood in your pee?", "gp",
       "Blood in your pee should be checked by a GP.", "NHS: Blood in urine"),
    _q("rash_spreading", "Is the redness spreading quickly or hot to touch?", "urgent",
       "Spreading, hot redness can be a skin infection needing treatment today.", "NHS: Cellulitis"),
    _q("swelling_face", "Is your face, lips, tongue or throat swelling?", "emergency",
       "Swelling of the face, lips or throat can be anaphylaxis.", "NHS: Anaphylaxis"),
    _q("vision", "Has your vision changed, or is the eye very painful?", "urgent",
       "Eye pain with vision changes needs to be seen today.", "NHS: Eye problems"),
    _q("numb_legs", "Any numbness around your bottom or genitals, or problems peeing or pooing?", "emergency",
       "These with back pain can be cauda equina syndrome, which needs A&E.", "NHS: Cauda equina syndrome"),
    _q("child_drowsy", "Is the child unusually drowsy, floppy or hard to wake?", "emergency",
       "A floppy or hard-to-wake child needs emergency help.", "NHS: Sepsis in children"),
    _q("child_fluids", "Are they drinking less, or having fewer wet nappies?", "urgent",
       "Drinking less or fewer wet nappies can mean dehydration.", "NHS: Dehydration"),
    _q("mood_safety", "Have you had thoughts of harming yourself or ending your life?", "emergency",
       "If you're having these thoughts, please talk to someone now.", "NHS: Help for suicidal thoughts"),
])

CATEGORIES: list[tuple[str, str, list[str]]] = [
    ("chest", r"chest|heart|palpitat", ["chest_spread", "sweaty_breathless", "breath_rest"]),
    ("breathing", r"breath|wheez|asthma|cough", ["breath_rest", "blue_lips", "fever", "very_unwell"]),
    ("head", r"headache|migraine|head|dizz", ["sudden_severe", "stiff_neck", "fast_signs", "head_injury", "fever"]),
    ("fever", r"fever|temperature|hot|shiver|flu|chills", ["fever", "very_unwell", "rash_glass", "stiff_neck"]),
    ("tummy", r"tummy|stomach|abdom|belly|vomit|diarr|sick|nause", ["lower_right", "cant_keep_fluids", "blood_vomit_poo", "fever", "pregnant"]),
    ("urinary", r"pee|wee|urin|bladder|cystitis", ["back_pain_fever", "blood_pee", "pregnant"]),
    ("skin", r"rash|skin|spot|itch|bite|sting|red\b|swell", ["rash_glass", "rash_spreading", "swelling_face", "fever"]),
    ("eye", r"\beyes?\b|vision|sight", ["vision", "head_injury"]),
    ("back", r"back pain|backache|lower back|sciatica", ["numb_legs", "back_pain_fever"]),
    ("mood", r"low mood|depress|anxi|panic|hopeless|can'?t cope|stress", ["mood_safety"]),
]
CHILD_QS = ["child_drowsy", "child_fluids"]
GENERAL = ["fever", "very_unwell"]


def plan(complaint: str, age_years: float | None = None, who: str = "me") -> dict:
    """Return the interview for this complaint (or flag an emergency immediately)."""
    text = complaint.lower()
    pre = evaluate(complaint, {"age_years": age_years} if age_years is not None else None)
    if pre.level == "emergency":
        return {"emergency": True, "category": None, "questions": [], "reasons": pre.reasons}

    ids: list[str] = []
    cats: list[str] = []
    for name, pattern, qs in CATEGORIES:
        if re.search(pattern, text):
            cats.append(name)
            ids += [q for q in qs if q not in ids]
    if not ids:
        ids = list(GENERAL)
    is_child = who == "child" or (age_years is not None and age_years < 12)
    if is_child:
        ids = [q for q in CHILD_QS if q not in ids] + ids
        ids = [q for q in ids if q != "pregnant"]
    if age_years is not None and (age_years < 12 or age_years > 55):
        ids = [q for q in ids if q != "pregnant"]
    ids = ids[:5]
    return {
        "emergency": False,
        "category": cats[0] if cats else "general",
        "questions": [{"id": q, "text": _for(QUESTIONS[q].text, who)} for q in ids],
        "reasons": [],
    }


_THIRD = [("Do you ", "Do they "), ("Are you ", "Are they "), ("Have you ", "Have they "),
          ("Could you ", "Could they "), ("Did you ", "Did they "), ("your ", "their "), ("Your ", "Their "),
          ("you've ", "they've "), ("you feel", "they feel"), (" you ", " they ")]


def _for(text: str, who: str) -> str:
    """Re-word a question for a child or someone else ("Do you..." -> "Do they...")."""
    if who == "me":
        return text
    for a, b in _THIRD:
        text = text.replace(a, b)
    return text
