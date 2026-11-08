"""
tests/test_journal.py - symptom journal + answer feedback endpoints.
"""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.auth.password import hash_password
from app.database import Base, get_db, get_redis
from app.models import AuthProvider, NotificationPrefs, User
from main import app

engine = create_async_engine(
    "sqlite+aiosqlite:///:memory:", connect_args={"check_same_thread": False}
)
Session = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)


async def _db():
    async with Session() as s:
        try:
            yield s
            await s.commit()
        except Exception:
            await s.rollback()
            raise


def _redis():
    m = AsyncMock()
    m.exists = AsyncMock(return_value=0)
    m.get = AsyncMock(return_value=None)
    pipe = AsyncMock()
    pipe.execute = AsyncMock(return_value=[1, True, None, True, []])
    for n in ("zremrangebyscore", "zcard", "zadd", "expire", "zrange"):
        setattr(pipe, n, MagicMock(return_value=None))
    m.pipeline = MagicMock(return_value=pipe)
    return m


@pytest_asyncio.fixture(autouse=True)
async def setup_db():
    async with engine.begin() as c:
        await c.run_sync(Base.metadata.create_all)
    yield
    async with engine.begin() as c:
        await c.run_sync(Base.metadata.drop_all)


@pytest_asyncio.fixture
async def client():
    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[get_redis] = _redis
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


async def _user(email="j@example.com", verified=True):
    async with Session() as db:
        u = User(
            email=email,
            full_name="Journal User",
            password_hash=hash_password("Test1234!"),
            auth_provider=AuthProvider.local,
            is_email_verified=verified,
            is_active=True,
        )
        db.add(u)
        await db.flush()
        db.add(NotificationPrefs(user_id=u.id))
        await db.commit()
        return u


async def _auth(client, email="j@example.com"):
    r = await client.post("/api/accounts/login", json={"email": email, "password": "Test1234!"})
    assert r.status_code == 200, r.text
    return {"Authorization": "Bearer " + r.json()["access_token"]}


# -- journal ---------------------------------------------------------------
async def test_requires_auth(client):
    r = await client.get("/api/journal")
    assert r.status_code == 401


async def test_requires_verified_email(client):
    await _user(verified=False)
    h = await _auth(client)
    r = await client.get("/api/journal", headers=h)
    assert r.status_code == 403


async def test_create_and_list(client):
    await _user()
    h = await _auth(client)
    r = await client.post(
        "/api/journal",
        headers=h,
        json={"symptom": "  Headache ", "severity": 6, "notes": "after work"},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["symptom"] == "Headache" and body["severity"] == 6
    r = await client.get("/api/journal", headers=h)
    assert r.json()["total"] == 1


async def test_validation(client):
    await _user()
    h = await _auth(client)
    assert (
        await client.post("/api/journal", headers=h, json={"symptom": "x", "severity": 11})
    ).status_code == 422
    assert (
        await client.post("/api/journal", headers=h, json={"symptom": "   ", "severity": 3})
    ).status_code == 422
    future = (datetime.now(UTC) + timedelta(days=2)).isoformat()
    r = await client.post(
        "/api/journal", headers=h, json={"symptom": "x", "severity": 3, "recorded_at": future}
    )
    assert r.status_code == 422


async def test_update_and_delete(client):
    await _user()
    h = await _auth(client)
    eid = (
        await client.post("/api/journal", headers=h, json={"symptom": "Cough", "severity": 4})
    ).json()["id"]
    r = await client.patch(
        f"/api/journal/{eid}", headers=h, json={"severity": 2, "notes": "better"}
    )
    assert r.status_code == 200 and r.json()["severity"] == 2 and r.json()["notes"] == "better"
    assert (await client.delete(f"/api/journal/{eid}", headers=h)).status_code == 204
    assert (await client.get("/api/journal", headers=h)).json()["total"] == 0


async def test_cannot_touch_other_users_entries(client):
    await _user("a@example.com")
    await _user("b@example.com")
    ha = await _auth(client, "a@example.com")
    hb = await _auth(client, "b@example.com")
    eid = (
        await client.post("/api/journal", headers=ha, json={"symptom": "Rash", "severity": 3})
    ).json()["id"]
    assert (
        await client.patch(f"/api/journal/{eid}", headers=hb, json={"severity": 9})
    ).status_code == 404
    assert (await client.delete(f"/api/journal/{eid}", headers=hb)).status_code == 404
    assert (await client.get("/api/journal", headers=hb)).json()["total"] == 0


async def test_days_filter_and_symptom_filter(client):
    await _user()
    h = await _auth(client)
    old = (datetime.now(UTC) - timedelta(days=40)).isoformat()
    await client.post(
        "/api/journal", headers=h, json={"symptom": "Back pain", "severity": 5, "recorded_at": old}
    )
    await client.post("/api/journal", headers=h, json={"symptom": "Back pain", "severity": 4})
    await client.post("/api/journal", headers=h, json={"symptom": "Cough", "severity": 2})
    assert (await client.get("/api/journal?days=30", headers=h)).json()["total"] == 2
    assert (await client.get("/api/journal?days=60", headers=h)).json()["total"] == 3
    assert (await client.get("/api/journal?days=60&symptom=back%20pain", headers=h)).json()[
        "total"
    ] == 2


async def test_summary_trends(client):
    await _user()
    h = await _auth(client)
    now = datetime.now(UTC)
    for i, sev in enumerate([8, 8, 7, 4, 3, 2]):  # improving headache
        ts = (now - timedelta(days=6 - i)).isoformat()
        await client.post(
            "/api/journal",
            headers=h,
            json={"symptom": "Headache", "severity": sev, "recorded_at": ts},
        )
    for i, sev in enumerate([2, 3, 6, 7]):  # worsening cough
        ts = (now - timedelta(days=4 - i, hours=1)).isoformat()
        await client.post(
            "/api/journal", headers=h, json={"symptom": "cough", "severity": sev, "recorded_at": ts}
        )
    s = (await client.get("/api/journal/summary?days=30", headers=h)).json()
    assert s["total_entries"] == 10
    by = {x["symptom"].lower(): x for x in s["symptoms"]}
    assert by["headache"]["trend"] == "improving"
    assert by["cough"]["trend"] == "worsening"
    assert by["headache"]["latest_severity"] == 2
    assert len(s["daily"]) >= 5


async def test_summary_empty(client):
    await _user()
    h = await _auth(client)
    s = (await client.get("/api/journal/summary", headers=h)).json()
    assert s["total_entries"] == 0 and s["avg_severity"] is None


# -- answer feedback -------------------------------------------------------
async def _chat(client, h):
    sid = (await client.post("/api/chat/sessions", headers=h)).json()["id"]
    r = await client.post(
        f"/api/chat/sessions/{sid}/messages",
        headers=h,
        json={"content": "I have a mild sore throat"},
    )
    assert r.status_code == 200, r.text
    return sid, r.json()


async def test_feedback_roundtrip(client):
    await _user()
    h = await _auth(client)
    sid, data = await _chat(client, h)
    mid = data["assistant_message"]["id"]
    r = await client.post(f"/api/chat/messages/{mid}/feedback", headers=h, json={"rating": "up"})
    assert r.status_code == 200 and r.json()["feedback"] == "up"
    detail = (await client.get(f"/api/chat/sessions/{sid}", headers=h)).json()
    assistant = [m for m in detail["messages"] if m["role"] == "assistant"][0]
    assert assistant["feedback"] == "up"
    r = await client.post(f"/api/chat/messages/{mid}/feedback", headers=h, json={"rating": None})
    assert r.json()["feedback"] is None


async def test_feedback_rejects_user_message_and_bad_rating(client):
    await _user()
    h = await _auth(client)
    _, data = await _chat(client, h)
    uid = data["user_message"]["id"]
    assert (
        await client.post(f"/api/chat/messages/{uid}/feedback", headers=h, json={"rating": "up"})
    ).status_code == 400
    mid = data["assistant_message"]["id"]
    assert (
        await client.post(f"/api/chat/messages/{mid}/feedback", headers=h, json={"rating": "meh"})
    ).status_code == 422


async def test_feedback_other_user_404(client):
    await _user("a@example.com")
    await _user("b@example.com")
    ha = await _auth(client, "a@example.com")
    hb = await _auth(client, "b@example.com")
    _, data = await _chat(client, ha)
    mid = data["assistant_message"]["id"]
    assert (
        await client.post(f"/api/chat/messages/{mid}/feedback", headers=hb, json={"rating": "down"})
    ).status_code == 404
