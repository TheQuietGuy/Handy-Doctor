"""
app/chat/ai/redflags.py - Deterministic red-flag rules (a safety floor).

Why rules as well as the language model?
  * LLM output changes with small wording changes; rules don't.
  * A published evaluation of a rule-based checker (Triage24) traced its
    unsafe results to (a) questions never asked and (b) *downgrading*
    urgency when the user denied symptoms.  So these rules can only ever
    RAISE the care level. A "no" answer never lowers anything.

Inputs
  text     - everything the user has written in this check
  context  - structured interview answers (who / age / duration / flags)

Output
  RuleResult(level, reasons) where level is the minimum safe care level.

Each rule cites the NHS page its threshold comes from.  These are a safety
net for a prototype and have NOT been clinically validated; see
docs/CLINICAL_SAFETY.md.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.chat.ai.care import highest, rank

NEG = re.compile(r"\b(no|not|without|never|denies|don'?t have|haven'?t got|isn'?t|aren'?t)\b[^.,;]{0,25}$")


@dataclass(frozen=True)
class TextRule:
    id: str
    level: str
    all_of: tuple[str, ...]          # every group must match (regex)
    reason: str
    source: str
    max_age: float | None = None     # only applies at or below this age (years)
    min_age: float | None = None


@dataclass
class RuleResult:
    level: str = "self_care"
    reasons: list[dict] = field(default_factory=list)

    def add(self, level: str, reason: str, source: str, rule_id: str) -> None:
        if any(r["id"] == rule_id for r in self.reasons):
            return
        self.reasons.append({"id": rule_id, "level": level, "reason": reason, "source": source})
        self.level = highest(self.level, level)


CHEST = r"chest (pain|tight|pressure|heav)|pain in (my|the) chest|crushing"
SPREAD = r"(arm|jaw|neck|back)|sweat|clammy|short of breath|breathless|sick|nause"

TEXT_RULES: tuple[TextRule, ...] = (
    # ── Emergency ─────────────────────────────────────────────────────────
    TextRule("heart_attack", "emergency", (CHEST, SPREAD),
             "Chest pain that spreads or comes with sweating or breathlessness can be a heart attack.",
             "NHS: Heart attack"),
    TextRule("chest_pain", "emergency", (r"(sudden|severe|crushing|heavy).{0,20}chest|chest.{0,20}(sudden|severe)",),
             "Sudden or severe chest pain needs emergency assessment.", "NHS: Chest pain"),
    TextRule("stroke", "emergency",
             (r"face.{0,15}(droop|drop|fall)|droop.{0,15}face|slurred speech|can'?t (lift|raise) (my|one|his|her) arm|arm.{0,10}weak|sudden(ly)? (numb|weak)|speech.{0,10}(slurred|garbled)",),
             "Face drooping, arm weakness or slurred speech are signs of a stroke (FAST).", "NHS: Stroke"),
    TextRule("anaphylaxis", "emergency",
             (r"(lips?|tongue|throat|face).{0,20}swell|swell.{0,20}(lips?|tongue|throat)|anaphyla",),
             "Swelling of the lips, tongue or throat can be anaphylaxis.", "NHS: Anaphylaxis"),
    TextRule("breathing", "emergency",
             (r"can'?t breathe|cannot breathe|struggling to breathe|gasping|blue (lips|skin)|lips.{0,10}blue|choking|not breathing",),
             "Severe difficulty breathing needs emergency help.", "NHS: Shortness of breath"),
    TextRule("meningitis_rash", "emergency",
             (r"rash.{0,40}(doesn'?t|does not|won'?t|didn'?t) fade|non.?blanching|rash.{0,30}glass",),
             "A rash that doesn't fade under a glass can be a sign of sepsis or meningitis.", "NHS: Meningitis"),
    TextRule("thunderclap", "emergency",
             (r"worst headache|sudden(,)? severe headache|thunderclap|headache.{0,30}(came on|started) suddenly",),
             "A sudden, severe 'worst ever' headache needs emergency assessment.", "NHS: Headaches"),
    TextRule("meningism", "emergency",
             (r"headache|fever|temperature", r"stiff neck|neck (is )?stiff|can'?t (bend|touch).{0,15}chin|light hurts|photophob"),
             "Headache or fever with a stiff neck or sensitivity to light can be meningitis.", "NHS: Meningitis"),
    TextRule("unconscious", "emergency",
             (r"unconscious|unresponsive|won'?t wake|passed out|collapsed|fitting|seizure|convuls",),
             "Collapse, fitting or not responding needs a 999 call.", "NHS: When to call 999"),
    TextRule("bleeding", "emergency",
             (r"(heavy|severe|won'?t stop|spurting|uncontrol\w*) bleed|bleeding (heavily|won'?t stop|a lot)|vomit\w* blood|coughing up blood",),
             "Heavy bleeding, or vomiting or coughing up blood, needs emergency care.", "NHS: When to call 999"),
    TextRule("head_injury", "emergency",
             (r"(hit|bang|banged|injur\w*|knock\w*|fell on).{0,20}head|head (injury|knock)", r"vomit|confus|drows|knocked out|seizure|clear fluid|blurred"),
             "A head injury with vomiting, confusion or drowsiness needs A&E.", "NHS: Head injury"),
    TextRule("vision_loss", "emergency",
             (r"sudden(ly)? (lost|loss of|can'?t see|blind)|lost (my |the )?(vision|sight)",),
             "Sudden loss of vision needs emergency assessment.", "NHS: Vision loss"),
    TextRule("testicle", "emergency", (r"(sudden|severe).{0,20}(testic|testicle|balls?)\b|(testic|testicle).{0,20}(sudden|severe)",),
             "Sudden, severe testicle pain can be torsion, which needs surgery within hours.", "NHS: Testicle pain"),
    TextRule("floppy_child", "emergency", (r"floppy|limp|won'?t wake|very drowsy|blue|mottled",),
             "A child who is floppy, very drowsy or mottled needs emergency help.", "NHS: Sepsis in children",
             max_age=12),
    # ── Urgent (today) ────────────────────────────────────────────────────
    TextRule("appendix", "urgent",
             (r"(lower )?right.{0,15}(side|tummy|abdomen|belly)|tummy pain.{0,30}right|pain.{0,20}lower right",
              r"pain|ache|hurt"),
             "Pain that settles in the lower right of the tummy needs checking today.", "NHS: Appendicitis"),
    TextRule("kidney", "urgent",
             (r"pee|urin|wee", r"fever|temperature|shiver|back pain|side pain|loin|vomit"),
             "Urinary symptoms with a temperature or back pain can mean a kidney infection.",
             "NHS: Kidney infection"),
    TextRule("pregnant_bleed", "urgent", (r"pregnan", r"bleed|spotting|severe pain|cramp"),
             "Bleeding or severe pain in pregnancy needs same-day advice.", "NHS: Vaginal bleeding in pregnancy"),
    TextRule("dehydrated_child", "urgent",
             (r"no wet nappies|not (weeing|peeing)|hasn'?t (weed|peed)|dry nappies|no tears|sunken",),
             "Signs of dehydration in a child need help today.", "NHS: Dehydration", max_age=12),
    TextRule("eye", "urgent", (r"eye", r"(very )?painful|blurred|light hurts|vision|chemical"),
             "A painful eye with vision changes needs to be seen today.", "NHS: Eye problems"),
    TextRule("cellulitis", "urgent", (r"skin|bite|wound|cut|graze|leg|arm|foot|hand", r"red|redness|hot|swollen", r"spread|getting bigger|streak|fever|temperature"),
             "Redness that is spreading or comes with a fever can be a skin infection needing treatment today.",
             "NHS: Cellulitis"),
    TextRule("high_fever_child", "urgent", (r"(39|40|41)(\.\d)?\s*(c|°c|degrees)",),
             "A temperature of 39°C or above in a young child needs advice today.", "NHS: Fever in children",
             max_age=5),
    # ── GP ────────────────────────────────────────────────────────────────
    TextRule("blood_in_pee_poo", "gp", (r"blood in (my )?(pee|urine|wee|poo|stool)|black (tarry )?(poo|stool)",),
             "Blood in your pee or poo should be checked by a GP.", "NHS: Blood in urine"),
    TextRule("weight_loss", "gp", (r"(lost|losing) weight without|unexplained weight loss|weight loss.{0,20}(not|without) trying",),
             "Unexplained weight loss should be checked by a GP.", "NHS: Unintentional weight loss"),
    TextRule("lump", "gp", (r"\blump\b|swelling that (won'?t|hasn'?t) gone",),
             "A new lump should be checked by a GP.", "NHS: Lumps"),
    # ── Pharmacy (Pharmacy First conditions) ──────────────────────────────
    TextRule("pharmacy_throat", "pharmacy", (r"sore throat|tonsil",),
             "Pharmacists can assess sore throats and supply treatment if needed (Pharmacy First).",
             "NHS: Pharmacy First"),
    TextRule("pharmacy_sinus", "pharmacy", (r"sinus",), "Pharmacists can help with sinusitis (Pharmacy First).",
             "NHS: Pharmacy First"),
    TextRule("pharmacy_ear", "pharmacy", (r"ear ?ache|ear (pain|infection)",),
             "Pharmacists can help with earache in children and young people (Pharmacy First).",
             "NHS: Pharmacy First", max_age=17, min_age=1),
    TextRule("pharmacy_bite", "pharmacy", (r"insect bite|bitten|sting",),
             "Pharmacists can treat infected insect bites (Pharmacy First).", "NHS: Pharmacy First"),
    TextRule("pharmacy_skin", "pharmacy", (r"impetigo|shingles|cold sore",),
             "Pharmacists can treat impetigo and shingles (Pharmacy First).", "NHS: Pharmacy First"),
    TextRule("pharmacy_uti", "pharmacy", (r"(pain|burn|sting)\w* when (i )?(pee|wee|urinat)|cystitis|uti\b",),
             "Pharmacists can treat uncomplicated urine infections in women aged 16 to 64 (Pharmacy First).",
             "NHS: Pharmacy First"),
)


def _matches(pattern: str, text: str) -> bool:
    for m in re.finditer(pattern, text):
        before = text[max(0, m.start() - 40): m.start()]
        if not NEG.search(before):
            return True
    return False


def _age(context: dict | None) -> float | None:
    if not context:
        return None
    try:
        a = context.get("age_years")
        return float(a) if a is not None and a != "" else None
    except (TypeError, ValueError):
        return None


def evaluate(text: str, context: dict | None = None) -> RuleResult:
    """Return the minimum safe care level for this text + interview context."""
    t = " " + re.sub(r"\s+", " ", text.lower()) + " "
    t = t.replace("’", "'")
    res = RuleResult()
    age = _age(context)

    for rule in TEXT_RULES:
        if rule.max_age is not None and (age is None or age > rule.max_age):
            continue
        if rule.min_age is not None and age is not None and age < rule.min_age:
            continue
        if all(_matches(p, t) for p in rule.all_of):
            res.add(rule.level, rule.reason, rule.source, rule.id)

    if context:
        _context_rules(context, age, t, res)
    return res


# Interview red-flag answers + structured context -----------------------------
def _context_rules(ctx: dict, age: float | None, text: str, res: RuleResult) -> None:
    from app.chat.ai.interview import QUESTIONS

    for qid, ans in (ctx.get("red_flags") or {}).items():
        q = QUESTIONS.get(qid)
        if q and ans is True:                      # only a YES can change anything
            res.add(q.level_if_yes, q.reason, q.source, "q:" + qid)

    fever = bool(re.search(r"fever|temperature|hot to touch|high temp", text)) or (ctx.get("red_flags") or {}).get("fever") is True
    if age is not None and age < 0.25 and fever:
        res.add("urgent", "Babies under 3 months with a temperature of 38°C or more need urgent advice.",
                "NHS: Fever in children", "infant_fever")
    elif age is not None and age < 0.5 and fever:
        res.add("gp", "Babies under 6 months with a temperature should be checked by a GP.",
                "NHS: Fever in children", "baby_fever")

    duration = ctx.get("duration")
    if duration == "over_3_weeks" and rank(res.level) < rank("gp"):
        res.add("gp", "Symptoms that have lasted more than 3 weeks should be checked by a GP.",
                "NHS: When to see a GP", "duration")

    sev = ctx.get("severity")
    if sev == "severe" and rank(res.level) < rank("urgent"):
        res.add("urgent", "You described the symptoms as severe, so we suggest getting help today.",
                "HandyDoctor safety rule", "severe")
    elif sev == "moderate" and duration in ("1_3_weeks", "over_3_weeks"):
        res.add("gp", "Moderate symptoms lasting over a week should be checked by a GP.",
                "HandyDoctor safety rule", "moderate_long")

    if ctx.get("pregnant") is True:
        res.add("gp", "Pregnancy changes what's safe to take. Speak to a GP, midwife or pharmacist first.",
                "NHS: Medicines in pregnancy", "pregnant")
