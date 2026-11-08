"""
tests/test_triage_rules.py - red-flag floor, care levels, guided interview, check-ins.
"""

import pytest

from app.chat.ai import care
from app.chat.ai.interview import QUESTIONS, plan
from app.chat.ai.pipeline import run_pipeline
from app.chat.ai.redflags import evaluate
from tests.test_journal import _auth, _user, client, setup_db  # noqa: F401  (fixtures)


# -- care scale ----------------------------------------------------------------
def test_care_scale_order_and_mapping():
    assert care.ORDER == ["self_care", "pharmacy", "gp", "urgent", "emergency"]
    assert care.highest("gp", None, "pharmacy") == "gp"
    assert care.raise_by("urgent") == "emergency" and care.raise_by("emergency") == "emergency"
    assert care.severity_of("pharmacy") == "green" and care.severity_of("urgent") == "amber"


# -- red-flag rules ------------------------------------------------------------
@pytest.mark.parametrize("text,level", [
    ("crushing chest pain going down my left arm", "emergency"),
    ("my face is drooping on one side and my speech is slurred", "emergency"),
    ("my lips are swelling after eating nuts", "emergency"),
    ("there's a rash that doesn't fade when I press a glass on it", "emergency"),
    ("worst headache of my life, came on suddenly", "emergency"),
    ("headache and fever and my neck is stiff", "emergency"),
    ("sharp pain in my lower right side since this morning", "urgent"),
    ("it burns when I pee and I have back pain and a temperature", "urgent"),
    ("I noticed blood in my pee", "gp"),
    ("sore throat for two days", "pharmacy"),
    ("a bit of a runny nose", "self_care"),
])
def test_text_rules(text, level):
    assert evaluate(text).level == level


def test_negation_does_not_trigger():
    assert evaluate("no chest pain, no stiff neck, just a sore throat").level == "pharmacy"


def test_child_rules_need_age():
    assert evaluate("he is floppy and very drowsy").level == "self_care"          # adult: no age rule
    assert evaluate("he is floppy and very drowsy", {"age_years": 2}).level == "emergency"


def test_infant_fever():
    assert evaluate("she has a temperature", {"age_years": 0.1}).level == "urgent"


def test_duration_and_severity_floor():
    assert evaluate("cough", {"duration": "over_3_weeks"}).level == "gp"
    assert evaluate("cough", {"severity": "severe"}).level == "urgent"


def test_yes_raises_but_no_never_lowers():
    yes = evaluate("headache", {"red_flags": {"sudden_severe": True}})
    assert yes.level == "emergency"
    no = evaluate("worst headache ever, sudden", {"red_flags": {"sudden_severe": False}})
    assert no.level == "emergency"          # a "no" can't undo what the text already said


def test_every_question_has_valid_level():
    assert all(q.level_if_yes in care.LEVELS for q in QUESTIONS.values())


# -- interview -------------------------------------------------------------------
def test_interview_emergency_skips_questions():
    p = plan("I have crushing chest pain spreading to my jaw")
    assert p["emergency"] is True and p["questions"] == []


def test_interview_picks_relevant_questions():
    ids = [q["id"] for q in plan("I've had a headache for three days")["questions"]]
    assert "sudden_severe" in ids and "stiff_neck" in ids and len(ids) <= 5


def test_interview_child_wording():
    qs = plan("my daughter has a temperature", 3, "child")["questions"]
    assert qs[0]["id"] == "child_drowsy"
    assert not any("pregnant" == q["id"] for q in qs)
    assert any("they" in q["text"] for q in qs)


# -- pipeline floor ------------------------------------------------------------
@pytest.mark.asyncio
async def test_pipeline_raises_to_rule_floor_and_explains():
    r = await run_pipeline("I have a headache", [], context={"red_flags": {"stiff_neck": True}})
    assert r.care_level == "emergency" and r.severity == "red"
    assert any(x["id"] == "q:stiff_neck" for x in r.reasons)


@pytest.mark.asyncio
async def test_pipeline_prepends_reason_when_raised():
    r = await run_pipeline("runny nose", [], context={"red_flags": {"very_unwell": True}})
    assert r.care_level == "urgent"
    assert r.content.startswith("**Get help today.**")
    r = await run_pipeline("mild cough", [], context={"duration": "over_3_weeks"})
    assert care.rank(r.care_level) >= care.rank("gp")


@pytest.mark.asyncio
async def test_pipeline_mood_flag_gives_crisis_response():
    r = await run_pipeline("feeling low", [], context={"red_flags": {"mood_safety": True}})
    assert "116 123" in r.content and r.care_level == "emergency"


# -- endpoints -----------------------------------------------------------------
async def test_interview_endpoint(client):  # noqa: F811
    await _user()
    h = await _auth(client)
    r = await client.post("/api/chat/interview", headers=h, json={"complaint": "tummy ache", "who": "me", "age_years": 30})
    assert r.status_code == 200
    body = r.json()
    assert body["emergency"] is False and body["category"] == "tummy"


async def test_message_with_context_and_checkin(client):  # noqa: F811
    await _user()
    h = await _auth(client)
    sid = (await client.post("/api/chat/sessions", headers=h)).json()["id"]
    r = await client.post(f"/api/chat/sessions/{sid}/messages", headers=h, json={
        "content": "I have a cough",
        "context": {"who": "me", "age_years": 40, "duration": "over_3_weeks", "severity": "mild", "red_flags": {"fever": False}},
    })
    assert r.status_code == 200, r.text
    a = r.json()["assistant_message"]
    assert a["care_level"] == "gp" and a["reasons"]
    assert r.json()["user_message"]["context"]["duration"] == "over_3_weeks"

    # Follow-up message inherits the interview context
    r = await client.post(f"/api/chat/sessions/{sid}/messages", headers=h, json={"content": "is honey ok?"})
    assert r.json()["assistant_message"]["care_level"] == "gp"

    # Check-in: worse raises the level and logs to the journal
    r = await client.post(f"/api/chat/sessions/{sid}/checkin", headers=h, json={"status": "worse", "severity": 7, "note": "coughing more"})
    assert r.status_code == 200, r.text
    assert r.json()["assistant_message"]["care_level"] == "urgent"
    assert r.json()["user_message"]["content"].startswith("Check-in: Feeling worse (7/10)")
    j = (await client.get("/api/journal", headers=h)).json()
    assert j["total"] == 1 and j["entries"][0]["severity"] == 7

    # better -> back to self-care unless the note has red flags
    r = await client.post(f"/api/chat/sessions/{sid}/checkin", headers=h, json={"status": "better"})
    assert r.json()["assistant_message"]["care_level"] == "self_care"
    r = await client.post(f"/api/chat/sessions/{sid}/checkin", headers=h, json={"status": "better", "note": "but my lips are swelling"})
    assert r.json()["assistant_message"]["care_level"] == "emergency"


async def test_checkin_same_steps_up_from_self_care(client):  # noqa: F811
    await _user()
    h = await _auth(client)
    sid = (await client.post("/api/chat/sessions", headers=h)).json()["id"]
    await client.post(f"/api/chat/sessions/{sid}/messages", headers=h, json={"content": "runny nose"})
    r = await client.post(f"/api/chat/sessions/{sid}/checkin", headers=h, json={"status": "same"})
    assert r.json()["assistant_message"]["care_level"] in ("pharmacy", "gp")


async def test_checkin_404_for_other_user(client):  # noqa: F811
    await _user("a@example.com")
    await _user("b@example.com")
    ha = await _auth(client, "a@example.com")
    hb = await _auth(client, "b@example.com")
    sid = (await client.post("/api/chat/sessions", headers=ha)).json()["id"]
    r = await client.post(f"/api/chat/sessions/{sid}/checkin", headers=hb, json={"status": "worse"})
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_no_adult_doses_for_children():
    r = await run_pipeline("my son has a cold and a temperature", [], context={"who": "child", "age_years": 3})
    assert "mg" not in r.content and "doses for children" in r.content
    adult = await run_pipeline("I have a cold and a temperature", [], context={"who": "me", "age_years": 30})
    assert "doses for children" not in adult.content
