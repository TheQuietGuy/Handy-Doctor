"""scripts/ingest/sources.py -- curated guideline sources."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class GuidelineSource:
    id: str
    url: str
    title: str
    authority: str
    topics: list[str] = field(default_factory=list)


SOURCES: list[GuidelineSource] = [
    GuidelineSource(
        "nhs_cold_flu",
        "https://www.nhs.uk/conditions/cold-flu/",
        "NHS: Cold and flu",
        "NHS",
        ["cold", "flu", "cough", "sore throat", "fever", "runny nose"],
    ),
    GuidelineSource(
        "nhs_sore_throat",
        "https://www.nhs.uk/conditions/sore-throat/",
        "NHS: Sore throat",
        "NHS",
        ["sore throat", "throat pain", "tonsillitis", "swallowing"],
    ),
    GuidelineSource(
        "nhs_headache",
        "https://www.nhs.uk/conditions/headache/",
        "NHS: Headaches",
        "NHS",
        ["headache", "head pain", "tension headache"],
    ),
    GuidelineSource(
        "nhs_migraine",
        "https://www.nhs.uk/conditions/migraine/",
        "NHS: Migraine",
        "NHS",
        ["migraine", "aura", "light sensitivity", "triptan"],
    ),
    GuidelineSource(
        "nhs_back_pain",
        "https://www.nhs.uk/conditions/back-pain/",
        "NHS: Back pain",
        "NHS",
        ["back pain", "lower back", "backache", "spine", "sciatica"],
    ),
    GuidelineSource(
        "nhs_chest_pain",
        "https://www.nhs.uk/conditions/chest-pain/",
        "NHS: Chest pain",
        "NHS",
        ["chest pain", "chest tightness", "heart", "angina", "palpitations"],
    ),
    GuidelineSource(
        "nhs_stomach_ache",
        "https://www.nhs.uk/conditions/stomach-ache/",
        "NHS: Stomach ache",
        "NHS",
        ["stomach ache", "abdominal pain", "belly", "cramping", "bloating"],
    ),
    GuidelineSource(
        "nhs_diarrhoea",
        "https://www.nhs.uk/conditions/diarrhoea-and-vomiting/",
        "NHS: Diarrhoea and vomiting",
        "NHS",
        ["diarrhoea", "vomiting", "gastroenteritis", "food poisoning", "nausea"],
    ),
    GuidelineSource(
        "nhs_uti",
        "https://www.nhs.uk/conditions/urinary-tract-infections-utis/",
        "NHS: Urinary tract infections",
        "NHS",
        ["uti", "cystitis", "burning urination", "bladder infection"],
    ),
    GuidelineSource(
        "nhs_rash",
        "https://www.nhs.uk/conditions/rashes-in-children/",
        "NHS: Rashes",
        "NHS",
        ["rash", "skin rash", "itching", "hives", "eczema"],
    ),
    GuidelineSource(
        "nhs_anxiety",
        "https://www.nhs.uk/mental-health/conditions/generalised-anxiety-disorder/overview/",
        "NHS: Anxiety",
        "NHS",
        ["anxiety", "worry", "panic attack", "stress", "GAD"],
    ),
    GuidelineSource(
        "nhs_depression",
        "https://www.nhs.uk/mental-health/conditions/depression-in-adults/overview/",
        "NHS: Depression",
        "NHS",
        ["depression", "low mood", "hopeless", "mental health"],
    ),
    GuidelineSource(
        "nhs_asthma",
        "https://www.nhs.uk/conditions/asthma/",
        "NHS: Asthma",
        "NHS",
        ["asthma", "wheezing", "breathless", "inhaler", "salbutamol"],
    ),
    GuidelineSource(
        "nhs_insomnia",
        "https://www.nhs.uk/conditions/insomnia/",
        "NHS: Insomnia",
        "NHS",
        ["insomnia", "sleep", "can't sleep", "tired", "fatigue"],
    ),
    GuidelineSource(
        "nhs_diabetes_type2",
        "https://www.nhs.uk/conditions/type-2-diabetes/",
        "NHS: Type 2 diabetes",
        "NHS",
        ["diabetes", "blood sugar", "glucose", "insulin"],
    ),
    GuidelineSource(
        "nhs_hypertension",
        "https://www.nhs.uk/conditions/high-blood-pressure-hypertension/",
        "NHS: High blood pressure",
        "NHS",
        ["high blood pressure", "hypertension", "blood pressure"],
    ),
    GuidelineSource(
        "nhs_stroke",
        "https://www.nhs.uk/conditions/stroke/",
        "NHS: Stroke",
        "NHS",
        ["stroke", "FAST", "face drooping", "arm weakness", "TIA"],
    ),
    GuidelineSource(
        "nhs_skin_conditions",
        "https://www.nhs.uk/conditions/skin-conditions/",
        "NHS: Skin conditions",
        "NHS",
        ["skin", "acne", "psoriasis", "eczema", "dry skin"],
    ),
    GuidelineSource(
        "nice_back_pain",
        "https://cks.nice.org.uk/topics/back-pain-low-without-radiculopathy/",
        "NICE CKS: Low back pain",
        "NICE",
        ["back pain", "lower back", "lumbar", "mechanical back pain"],
    ),
    GuidelineSource(
        "nice_headache",
        "https://cks.nice.org.uk/topics/headache-assessment/",
        "NICE CKS: Headache assessment",
        "NICE",
        ["headache", "migraine", "tension headache", "thunderclap"],
    ),
    GuidelineSource(
        "nice_uti",
        "https://cks.nice.org.uk/topics/urinary-tract-infection-lower-women/",
        "NICE CKS: UTI (lower) in women",
        "NICE",
        ["uti women", "cystitis", "nitrofurantoin", "trimethoprim"],
    ),
    GuidelineSource(
        "nice_anxiety",
        "https://cks.nice.org.uk/topics/generalized-anxiety-disorder/",
        "NICE CKS: Generalised anxiety disorder",
        "NICE",
        ["anxiety disorder", "GAD", "CBT anxiety", "sertraline"],
    ),
    GuidelineSource(
        "nice_depression",
        "https://cks.nice.org.uk/topics/depression/",
        "NICE CKS: Depression",
        "NICE",
        ["depression treatment", "antidepressant", "SSRI", "CBT depression"],
    ),
    GuidelineSource(
        "nice_asthma",
        "https://cks.nice.org.uk/topics/asthma/",
        "NICE CKS: Asthma",
        "NICE",
        ["asthma management", "inhaler technique", "SABA", "peak flow"],
    ),
    GuidelineSource(
        "who_mental_health",
        "https://www.who.int/news-room/fact-sheets/detail/mental-health-strengthening-our-response",
        "WHO: Mental health",
        "WHO",
        ["mental health", "wellbeing", "mental disorder", "suicide prevention"],
    ),
    GuidelineSource(
        "who_hypertension",
        "https://www.who.int/news-room/fact-sheets/detail/hypertension",
        "WHO: Hypertension",
        "WHO",
        ["hypertension", "blood pressure", "cardiovascular", "stroke risk"],
    ),
]

SOURCE_MAP: dict[str, GuidelineSource] = {s.id: s for s in SOURCES}
