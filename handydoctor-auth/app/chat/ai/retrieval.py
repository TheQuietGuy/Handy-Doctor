"""
app/chat/ai/retrieval.py — Retrieval layer for the RAG pipeline.

Architecture
────────────
BaseRetriever       Abstract interface — swap implementations without
                    touching the pipeline.

MockRetriever       Zero-dependency implementation used in development
                    and tests.  Scores each chunk in the knowledge base
                    by counting keyword overlaps with the query, returns
                    the top-k highest-scoring chunks.  Different queries
                    genuinely return different, relevant context — it's
                    not just random rotation.

ChromaRetriever     Stub for a real vector-store integration (Chroma).
                    Swap in by setting VECTOR_STORE=chroma in .env and
                    providing CHROMA_HOST / OPENAI_API_KEY.

Each retrieved chunk carries:
  • content  — the raw guideline text used to ground the LLM prompt
  • source   — human-readable citation ("NHS: Chest pain")
  • url      — link to the original guideline
  • score    — relevance score (0–1)
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class Chunk:
    content: str
    source: str
    url: str
    score: float = 0.0


class BaseRetriever(ABC):
    @abstractmethod
    async def retrieve(self, query: str, top_k: int = 3) -> list[Chunk]:
        """Return the top-k most relevant chunks for *query*."""


# ── Knowledge base ────────────────────────────────────────────────────────────
_KB: list[dict] = [
    {
        "keywords": [
            "cold",
            "flu",
            "runny nose",
            "sneezing",
            "sore throat",
            "cough",
            "fever",
            "temperature",
        ],
        "content": (
            "For colds and flu, NHS recommends rest and adequate fluid intake. "
            "Paracetamol (500–1000 mg every 4–6 hours, max 4 g/day) or ibuprofen "
            "(400 mg every 6–8 hours with food) can relieve fever and aches. "
            "Antibiotics are not effective and are not recommended. "
            "Symptoms typically resolve within 7–10 days. Seek medical attention "
            "if symptoms worsen significantly after 3 days, you develop chest pain, "
            "difficulty breathing, or high fever (>39°C) unresponsive to paracetamol."
        ),
        "source": "NHS: Cold and flu self-care",
        "url": "https://www.nhs.uk/conditions/cold-flu/",
    },
    {
        "keywords": [
            "chest pain",
            "chest tightness",
            "heart",
            "angina",
            "palpitation",
            "heart attack",
        ],
        "content": (
            "Chest pain can range from minor musculoskeletal discomfort to a "
            "life-threatening emergency. Call 999 immediately if chest pain is "
            "accompanied by: pain radiating to the arm, jaw, or back; shortness "
            "of breath; sweating; nausea; or a feeling of dread. "
            "Stable angina (predictable exertional chest pain relieved by rest "
            "within 10 minutes) should be assessed by a GP urgently. "
            "NICE recommends GTN spray for established angina and urgent referral "
            "for any new or changing chest pain pattern."
        ),
        "source": "NHS: Chest pain — when to seek help",
        "url": "https://www.nhs.uk/conditions/chest-pain/",
    },
    {
        "keywords": ["headache", "migraine", "head pain", "throbbing", "tension headache"],
        "content": (
            "Most headaches are tension-type or migraine and can be managed "
            "with over-the-counter analgesia. Paracetamol or ibuprofen taken "
            "early in an attack are first-line. NICE recommends triptan therapy "
            "for established migraine unresponsive to OTC analgesia. "
            "Seek urgent medical attention ('thunderclap' presentation) if headache "
            "is the worst of your life, comes on suddenly, is associated with fever "
            "and neck stiffness, follows head trauma, or is accompanied by confusion, "
            "vision changes, or weakness — these may indicate subarachnoid haemorrhage "
            "or meningitis."
        ),
        "source": "NICE CKS: Headache — assessment",
        "url": "https://cks.nice.org.uk/topics/headache-assessment/",
    },
    {
        "keywords": [
            "stomach",
            "abdominal pain",
            "stomach ache",
            "nausea",
            "vomiting",
            "diarrhoea",
            "gastro",
        ],
        "content": (
            "Acute gastroenteritis is usually self-limiting (2–5 days). "
            "Oral rehydration solution (ORS) is the cornerstone of management — "
            "drink small sips frequently. Avoid anti-diarrhoeal agents in children. "
            "Loperamide may shorten symptom duration in adults. "
            "See a GP if: symptoms persist beyond 7 days, blood in stool, "
            "fever above 38°C, signs of dehydration (dark urine, dizziness), "
            "or if you are immunocompromised. Severe abdominal pain with guarding "
            "may indicate appendicitis or peritonitis — seek emergency care."
        ),
        "source": "NHS: Stomach ache and abdominal pain",
        "url": "https://www.nhs.uk/conditions/stomach-ache/",
    },
    {
        "keywords": ["rash", "skin", "eczema", "hives", "urticaria", "itching", "allergic"],
        "content": (
            "Urticaria (hives) is usually self-limiting. Non-sedating antihistamines "
            "(cetirizine 10 mg or loratadine 10 mg daily) are first-line. "
            "Avoid known triggers. Eczema flares respond to emollients applied "
            "frequently and mild topical corticosteroids (1% hydrocortisone) for "
            "short courses on affected areas. "
            "Seek emergency care if a rash is accompanied by throat swelling, "
            "difficulty breathing, or swelling of the lips/tongue — this may "
            "indicate anaphylaxis. Call 999 if a non-blanching petechial rash "
            "with fever is present (possible meningococcal disease)."
        ),
        "source": "NICE CKS: Urticaria and eczema management",
        "url": "https://cks.nice.org.uk/topics/urticaria/",
    },
    {
        "keywords": ["back pain", "back ache", "lower back", "spine", "sciatica"],
        "content": (
            "Most acute low back pain resolves within 6 weeks with conservative management. "
            "NICE recommends staying active, self-management education, and analgesia "
            "(paracetamol ± ibuprofen) as first-line. Bed rest is not recommended. "
            "Consider physiotherapy if not improving after 4–6 weeks. "
            "Red flags requiring urgent assessment: pain with bladder/bowel dysfunction "
            "(cauda equina syndrome), pain in someone with cancer history, progressive "
            "neurological deficit, fever with back pain (discitis), or pain in "
            "children under 20."
        ),
        "source": "NICE NG59: Low back pain and sciatica",
        "url": "https://www.nice.org.uk/guidance/ng59",
    },
    {
        "keywords": [
            "breathless",
            "shortness of breath",
            "breathing",
            "asthma",
            "wheezing",
            "inhaler",
        ],
        "content": (
            "Shortness of breath at rest or with minimal exertion requires prompt assessment. "
            "For known asthmatics: use reliever inhaler (salbutamol 2–4 puffs) immediately; "
            "if no improvement after 10–15 minutes repeat and seek emergency care. "
            "NICE recommends spacer devices with pMDI inhalers. "
            "New-onset breathlessness, especially with cough, fever, or pleuritic pain, "
            "may indicate pneumonia or pulmonary embolism — seek same-day medical review. "
            "Severe breathlessness, cyanosis, or inability to complete sentences: call 999."
        ),
        "source": "NICE BTS/SIGN: Asthma and breathlessness guidelines",
        "url": "https://www.brit-thoracic.org.uk/quality-improvement/guidelines/asthma/",
    },
    {
        "keywords": [
            "anxiety",
            "stress",
            "panic",
            "panic attack",
            "mental health",
            "depression",
            "low mood",
        ],
        "content": (
            "Anxiety and low mood are very common and highly treatable. "
            "NICE recommends stepped care: guided self-help and psychological therapies "
            "(CBT) as first-line for mild-to-moderate anxiety/depression. "
            "SSRIs (e.g. sertraline) are first-line pharmacological treatment. "
            "NHS Talking Therapies (IAPT) offers free CBT — self-refer at nhs.uk/talk. "
            "Lifestyle measures with strong evidence: regular aerobic exercise (150 min/wk), "
            "sleep hygiene, and limiting alcohol. "
            "If you are having thoughts of self-harm or suicide, contact Samaritans "
            "116 123 (free, 24/7) or call 999 if in immediate danger."
        ),
        "source": "NICE CKS: Depression and anxiety management",
        "url": "https://cks.nice.org.uk/topics/depression/",
    },
    {
        "keywords": [
            "urinary",
            "uti",
            "cystitis",
            "burning urine",
            "frequent urination",
            "bladder",
        ],
        "content": (
            "Uncomplicated urinary tract infection (UTI) in women can be managed "
            "with empirical antibiotic therapy: nitrofurantoin 100 mg MR BD for 5 days "
            "or trimethoprim 200 mg BD for 7 days (NICE NG112). "
            "Increase fluid intake. Cranberry products have limited evidence. "
            "Symptoms usually resolve within 2–3 days of antibiotics. "
            "Seek urgent GP review if: fever, loin pain, rigors, or vomiting "
            "(suggests pyelonephritis); if male; if symptoms do not improve after 48h; "
            "or if pregnant. Recurrent UTIs (≥2/6 months) warrant investigation."
        ),
        "source": "NICE NG112: Urinary tract infection (lower)",
        "url": "https://www.nice.org.uk/guidance/ng112",
    },
    {
        "keywords": ["sleep", "insomnia", "tired", "fatigue", "can't sleep", "exhausted"],
        "content": (
            "Insomnia affects around 1 in 3 people. NICE recommends cognitive "
            "behavioural therapy for insomnia (CBT-I) as first-line — more effective "
            "than medication long-term. Sleep hygiene measures: consistent sleep/wake "
            "times, cool dark bedroom, no screens 1h before bed, limit caffeine after noon. "
            "Short-term hypnotics (e.g. zopiclone) should only be prescribed for "
            "severe insomnia and for no more than 4 weeks. "
            "Persistent fatigue lasting >4 weeks with no clear cause warrants GP review "
            "to exclude anaemia, thyroid disease, diabetes, or depression."
        ),
        "source": "NICE CKS: Insomnia and sleep disorders",
        "url": "https://cks.nice.org.uk/topics/insomnia/",
    },
]


# ── Mock retriever ────────────────────────────────────────────────────────────
class MockRetriever(BaseRetriever):
    """
    Keyword-overlap retriever.  Tokenises the query and each chunk's keyword
    list, counts overlaps, and returns the top-k chunks by score.

    Good enough for development and testing — produces contextually relevant
    responses (a chest-pain query gets cardiac guidelines, not UTI guidelines).
    """

    async def retrieve(self, query: str, top_k: int = 3) -> list[Chunk]:
        query_tokens = set(_tokenise(query))
        scored: list[Chunk] = []

        for entry in _KB:
            kw_tokens = set(_tokenise(" ".join(entry["keywords"])))
            overlap = len(query_tokens & kw_tokens)
            if overlap == 0:
                continue
            score = overlap / max(len(query_tokens), 1)
            scored.append(
                Chunk(
                    content=entry["content"],
                    source=entry["source"],
                    url=entry["url"],
                    score=score,
                )
            )

        scored.sort(key=lambda c: c.score, reverse=True)

        # Always return at least one chunk (most general) even with no overlap
        if not scored:
            fallback = _KB[0]
            return [
                Chunk(
                    content=fallback["content"],
                    source=fallback["source"],
                    url=fallback["url"],
                    score=0.0,
                )
            ]

        return scored[:top_k]


def _tokenise(text: str) -> list[str]:
    """Lowercase, strip punctuation, split on whitespace."""
    return re.sub(r"[^\w\s]", "", text.lower()).split()


# ── Chroma retriever stub (swap in when ready) ────────────────────────────────
class ChromaRetriever(BaseRetriever):
    """
    Real vector-store retriever backed by the indexed NHS/NICE/WHO collection.

    Requirements
    ------------
    1. Run the ingestion pipeline:
           python scripts/ingest/run.py --all
    2. Start Chroma:
           docker run -p 8001:8000 chromadb/chroma
    3. Set environment variables:
           CHROMA_HOST=localhost   (or the docker service name in prod)
           CHROMA_PORT=8001
           OPENAI_API_KEY=sk-...   (optional; falls back to TF-IDF)
    """

    def __init__(self) -> None:
        import os

        from scripts.ingest.embedder import get_embedder
        from scripts.ingest.store import VectorStore

        self._store = VectorStore(
            host=os.environ.get("CHROMA_HOST", "localhost"),
            port=int(os.environ.get("CHROMA_PORT", "8001")),
        )
        self._embedder = get_embedder()

    async def retrieve(self, query: str, top_k: int = 3) -> list[Chunk]:
        embedding = self._embedder.embed_query(query)
        results = self._store.query(embedding, top_k=top_k)
        return [Chunk(content=r.text, source=r.title, url=r.url, score=r.score) for r in results]
