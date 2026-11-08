"""
app/chat/ai/care.py - Five-level care scale.

Mirrors how NHS 111 routes people to a *service*, not just a colour:

  rank  level       label                         maps to severity  care map filter
  0     self_care   Look after yourself at home   green             -
  1     pharmacy    Ask a pharmacist              green             pharm
  2     gp          Contact your GP               amber             gp
  3     urgent      Get help today                amber             ae (UTC / A&E list)
  4     emergency   Call 999 or go to A&E         red               ae

The three-colour `severity` is kept for backwards compatibility (emails,
admin stats, old clients). New clients read `care_level`.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CareLevel:
    key: str
    rank: int
    label: str
    action: str
    severity: str
    care_filter: str | None


LEVELS: dict[str, CareLevel] = {
    "self_care": CareLevel(
        "self_care", 0, "Look after yourself at home",
        "Rest and use the self-care advice below. Get help if things change.", "green", None,
    ),
    "pharmacy": CareLevel(
        "pharmacy", 1, "Ask a pharmacist",
        "A pharmacist can advise and, for some conditions, treat you without a GP appointment.",
        "green", "pharm",
    ),
    "gp": CareLevel(
        "gp", 2, "Contact your GP",
        "Book a GP appointment in the next few days. Call 111 if you can't get one and you're worried.",
        "amber", "gp",
    ),
    "urgent": CareLevel(
        "urgent", 3, "Get help today",
        "Call NHS 111 or your GP surgery today, or go to an urgent treatment centre.",
        "amber", "ae",
    ),
    "emergency": CareLevel(
        "emergency", 4, "Call 999 or go to A&E",
        "This needs emergency care now. Call 999 or go to your nearest A&E.", "red", "ae",
    ),
}
ORDER = sorted(LEVELS, key=lambda k: LEVELS[k].rank)


def rank(level: str | None) -> int:
    return LEVELS[level].rank if level in LEVELS else -1


def highest(*levels: str | None) -> str:
    """Return the most urgent of the given levels (None values ignored)."""
    present = [lv for lv in levels if lv in LEVELS]
    return max(present, key=rank) if present else "self_care"


def raise_by(level: str, steps: int = 1) -> str:
    return ORDER[min(len(ORDER) - 1, rank(level) + steps)]


def severity_of(level: str) -> str:
    return LEVELS.get(level, LEVELS["self_care"]).severity


def from_response_text(content: str, severity: str) -> str:
    """Map the generator's 3-colour severity + wording onto the 5-level scale."""
    lower = content.lower()
    if severity == "red":
        return "emergency"
    if severity == "amber":
        if any(k in lower for k in ("today", "same day", "same-day", "call 111 now", "call nhs 111 now",
                                    "urgent treatment", "within 24 hours")):
            return "urgent"
        return "gp"
    if "pharmacist" in lower or "pharmacy" in lower:
        return "pharmacy"
    return "self_care"
