"""
tests/test_chat.py — Test suite for the chat session and RAG pipeline component.
"""

import json
import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.auth.password import hash_password
from app.chat import service
from app.chat.ai.pipeline import run_pipeline
from app.chat.ai.retrieval import MockRetriever
from app.chat.ai.triage import classify_query, classify_response, severity_badge
from app.chat.models import MessageRole
from app.database import Base, get_db, get_redis
from app.models import AuthProvider, NotificationPrefs, User
from main import app

TEST_DB_URL = "sqlite+aiosqlite:///:memory:"
test_engine = create_async_engine(TEST_DB_URL, connect_args={"check_same_thread": False})
TestSession = async_sessionmaker(test_engine, expire_on_commit=False, autoflush=False)


async def override_db():
    async with TestSession() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


def _make_redis():
    m = AsyncMock()
    m.exists = AsyncMock(return_value=0)
    m.incr = AsyncMock(return_value=1)
    m.expire = AsyncMock(return_value=True)
    m.set = AsyncMock(return_value=True)
    m.get = AsyncMock(return_value=None)
    m.delete = AsyncMock(return_value=1)
    m.ttl = AsyncMock(return_value=-2)
    pipe = AsyncMock()
    pipe.execute = AsyncMock(return_value=[1, True, None, True, []])
    pipe.zremrangebyscore = MagicMock(return_value=None)
    pipe.zcard = MagicMock(return_value=None)
    pipe.zadd = MagicMock(return_value=None)
    pipe.expire = MagicMock(return_value=None)
    pipe.zrange = MagicMock(return_value=None)
    m.pipeline = MagicMock(return_value=pipe)
    return m


@pytest_asyncio.fixture(autouse=True)
async def setup_db():
    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest_asyncio.fixture
async def client():
    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_redis] = lambda: _make_redis()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


@pytest_asyncio.fixture
async def db():
    async with TestSession() as session:
        yield session
        await session.rollback()


async def _make_user(db: AsyncSession, email: str = "chat@example.com") -> User:
    user = User(
        email=email,
        full_name="Chat User",
        password_hash=hash_password("Test1234!"),
        auth_provider=AuthProvider.local,
        is_email_verified=True,
        is_active=True,
    )
    db.add(user)
    await db.flush()  # materialise user.id before FK reference
    db.add(NotificationPrefs(user_id=user.id))
    await db.commit()
    await db.refresh(user)
    return user


async def _login(client, email="chat@example.com", password="Test1234!") -> str:
    resp = await client.post("/api/accounts/login", json={"email": email, "password": password})
    assert resp.status_code == 200, resp.text
    return resp.json()["access_token"]


# ════════════════════════════════════════════════════════════════════════════
# 1. TRIAGE CLASSIFIER
# ════════════════════════════════════════════════════════════════════════════
class TestTriageClassifier:
    def test_emergency_query_red(self):
        assert classify_query("I have chest pain and can't breathe") == "red"

    def test_normal_query_none(self):
        assert classify_query("I have a mild headache") is None

    def test_response_red_on_call_999(self):
        assert classify_response("You must call 999 immediately.") == "red"

    def test_response_amber_on_see_gp(self):
        assert classify_response("You should see a GP soon for this.") == "amber"

    def test_response_green_on_no_signals(self):
        assert classify_response("Rest and drink plenty of water.") == "green"

    def test_severity_badge_green(self):
        assert "Self-care" in severity_badge("green")

    def test_severity_badge_amber(self):
        assert "GP" in severity_badge("amber")

    def test_severity_badge_red(self):
        assert "999" in severity_badge("red")

    def test_severity_badge_fallback(self):
        assert "Self-care" in severity_badge("unknown")


# ════════════════════════════════════════════════════════════════════════════
# 2. MOCK RETRIEVER
# ════════════════════════════════════════════════════════════════════════════
class TestMockRetriever:
    @pytest.mark.asyncio
    async def test_chest_pain_returns_cardiac_chunk(self):
        chunks = await MockRetriever().retrieve("I have severe chest pain", top_k=3)
        assert len(chunks) >= 1
        sources = " ".join(c.source for c in chunks).lower()
        assert "chest" in sources or "heart" in sources or "cardiac" in sources

    @pytest.mark.asyncio
    async def test_headache_returns_relevant_chunk(self):
        chunks = await MockRetriever().retrieve("severe headache throbbing pain", top_k=3)
        combined = " ".join(c.source + c.content for c in chunks).lower()
        assert "headache" in combined or "migraine" in combined

    @pytest.mark.asyncio
    async def test_top_k_respected(self):
        chunks = await MockRetriever().retrieve("cough cold flu fever sore throat", top_k=2)
        assert len(chunks) <= 2

    @pytest.mark.asyncio
    async def test_unknown_query_returns_fallback(self):
        chunks = await MockRetriever().retrieve("xyzzy frobnicator plugh", top_k=3)
        assert len(chunks) >= 1

    @pytest.mark.asyncio
    async def test_chunks_have_required_fields(self):
        chunks = await MockRetriever().retrieve("stomach ache diarrhoea", top_k=1)
        for c in chunks:
            assert c.content
            assert c.source
            assert isinstance(c.score, float)

    @pytest.mark.asyncio
    async def test_different_queries_return_different_primary_chunks(self):
        cold_chunks = await MockRetriever().retrieve("runny nose sneezing cough cold", top_k=1)
        back_chunks = await MockRetriever().retrieve("lower back pain sciatica spine", top_k=1)
        assert cold_chunks[0].source != back_chunks[0].source


# ════════════════════════════════════════════════════════════════════════════
# 3. RAG PIPELINE
# ════════════════════════════════════════════════════════════════════════════
class TestRAGPipeline:
    @pytest.mark.asyncio
    async def test_normal_query_returns_response(self):
        result = await run_pipeline("I have a mild headache", history=[])
        assert result.content
        assert result.severity in ("green", "amber", "red")
        assert not result.is_emergency

    @pytest.mark.asyncio
    async def test_emergency_query_short_circuits(self):
        result = await run_pipeline("I have chest pain and can't breathe", history=[])
        assert result.is_emergency
        assert result.severity == "red"
        assert "999" in result.content or "A&E" in result.content

    @pytest.mark.asyncio
    async def test_crisis_query_short_circuits(self):
        result = await run_pipeline("I'm having suicidal thoughts", history=[])
        assert result.is_emergency
        assert result.severity == "red"
        assert "Samaritans" in result.content or "116" in result.content

    @pytest.mark.asyncio
    async def test_citations_populated_for_normal_query(self):
        result = await run_pipeline("I have a sore throat and cough", history=[])
        if not result.is_emergency:
            assert len(result.citations) >= 1

    @pytest.mark.asyncio
    async def test_disclaimer_in_response(self):
        result = await run_pipeline("I have a mild stomach ache", history=[])
        low = result.content.lower()
        assert "substitute" in low or "medical advice" in low or "professional" in low

    @pytest.mark.asyncio
    async def test_pipeline_handles_history(self):
        history = [
            {"role": "user", "content": "I have a headache"},
            {"role": "assistant", "content": "Rest and take paracetamol."},
        ]
        result = await run_pipeline("Is ibuprofen safe to take too?", history=history)
        assert result.content

    @pytest.mark.asyncio
    async def test_empty_query_still_responds(self):
        result = await run_pipeline("  ", history=[])
        assert result.content


# ════════════════════════════════════════════════════════════════════════════
# 4. SERVICE LAYER
# ════════════════════════════════════════════════════════════════════════════
class TestChatService:
    @pytest.mark.asyncio
    async def test_create_session(self, db):
        user = await _make_user(db)
        session = await service.create_session(db, user.id)
        await db.commit()
        assert session.id is not None
        assert session.title == "New chat"
        assert session.message_count == 0

    @pytest.mark.asyncio
    async def test_get_session_owned(self, db):
        user = await _make_user(db)
        s = await service.create_session(db, user.id)
        await db.commit()
        fetched = await service.get_session(db, s.id, user.id)
        assert fetched is not None

    @pytest.mark.asyncio
    async def test_get_session_rejects_wrong_owner(self, db):
        u1 = await _make_user(db, "a@x.com")
        u2 = await _make_user(db, "b@x.com")
        s = await service.create_session(db, u1.id)
        await db.commit()
        assert await service.get_session(db, s.id, u2.id) is None

    @pytest.mark.asyncio
    async def test_list_sessions_pagination(self, db):
        user = await _make_user(db)
        for _ in range(5):
            await service.create_session(db, user.id)
        await db.commit()
        page1, total = await service.list_sessions(db, user.id, offset=0, limit=3)
        assert total == 5
        assert len(page1) == 3
        page2, _ = await service.list_sessions(db, user.id, offset=3, limit=3)
        assert len(page2) == 2

    @pytest.mark.asyncio
    async def test_add_message_returns_pair(self, db):
        user = await _make_user(db)
        s = await service.create_session(db, user.id)
        await db.commit()
        result = await service.add_message(db, s.id, user.id, "I have a headache")
        await db.commit()
        assert result is not None
        user_msg, asst_msg = result
        assert user_msg.role == MessageRole.user
        assert asst_msg.role == MessageRole.assistant
        assert asst_msg.severity in ("green", "amber", "red")

    @pytest.mark.asyncio
    async def test_add_message_increments_count(self, db):
        user = await _make_user(db)
        s = await service.create_session(db, user.id)
        await db.commit()
        await service.add_message(db, s.id, user.id, "headache")
        await db.commit()
        refreshed = await service.get_session(db, s.id, user.id)
        assert refreshed.message_count == 2

    @pytest.mark.asyncio
    async def test_add_message_sets_title(self, db):
        user = await _make_user(db)
        s = await service.create_session(db, user.id)
        await db.commit()
        await service.add_message(db, s.id, user.id, "I have a sore throat")
        await db.commit()
        refreshed = await service.get_session(db, s.id, user.id)
        assert refreshed.title != "New chat"

    @pytest.mark.asyncio
    async def test_title_not_overwritten_on_second_message(self, db):
        user = await _make_user(db)
        s = await service.create_session(db, user.id)
        await db.commit()
        await service.add_message(db, s.id, user.id, "I have a sore throat")
        await db.commit()
        title_after_first = (await service.get_session(db, s.id, user.id)).title
        await service.add_message(db, s.id, user.id, "The pain is getting worse")
        await db.commit()
        title_after_second = (await service.get_session(db, s.id, user.id)).title
        assert title_after_first == title_after_second

    @pytest.mark.asyncio
    async def test_citations_stored_as_json(self, db):
        user = await _make_user(db)
        s = await service.create_session(db, user.id)
        await db.commit()
        _, asst = await service.add_message(db, s.id, user.id, "back pain")
        await db.commit()
        if not asst.citations:
            return
        parsed = json.loads(asst.citations)
        assert isinstance(parsed, list)
        for cit in parsed:
            assert "source" in cit

    @pytest.mark.asyncio
    async def test_delete_session(self, db):
        user = await _make_user(db)
        s = await service.create_session(db, user.id)
        await db.commit()
        assert await service.delete_session(db, s.id, user.id) is True
        await db.commit()
        assert await service.get_session(db, s.id, user.id) is None

    @pytest.mark.asyncio
    async def test_delete_wrong_owner_returns_false(self, db):
        u1 = await _make_user(db, "c@x.com")
        u2 = await _make_user(db, "d@x.com")
        s = await service.create_session(db, u1.id)
        await db.commit()
        assert await service.delete_session(db, s.id, u2.id) is False

    @pytest.mark.asyncio
    async def test_export_json_structure(self, db):
        user = await _make_user(db)
        s = await service.create_session(db, user.id)
        await db.commit()
        await service.add_message(db, s.id, user.id, "I have a rash")
        await db.commit()
        # Use a fresh session to avoid identity-map staleness after expire_all
        async with TestSession() as fresh_db:
            export = await service.export_session_json(fresh_db, s.id, user.id)
        assert export is not None
        assert export["export_format"] == "handydoctor_chat_v1"
        assert len(export["messages"]) == 2
        assert "disclaimer" in export

    @pytest.mark.asyncio
    async def test_add_message_nonexistent_session(self, db):
        user = await _make_user(db)
        result = await service.add_message(db, uuid.uuid4(), user.id, "hello")
        assert result is None


# ════════════════════════════════════════════════════════════════════════════
# 5. HTTP ROUTER
# ════════════════════════════════════════════════════════════════════════════
class TestChatRouter:
    @pytest.mark.asyncio
    async def test_create_session_requires_auth(self, client):
        resp = await client.post("/api/chat/sessions")
        assert resp.status_code in (401, 403)  # HTTPBearer returns 401

    @pytest.mark.asyncio
    async def test_create_session_success(self, client):
        async with TestSession() as db:
            await _make_user(db)
        token = await _login(client)
        resp = await client.post("/api/chat/sessions", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 201
        assert resp.json()["message_count"] == 0

    @pytest.mark.asyncio
    async def test_list_sessions_empty(self, client):
        async with TestSession() as db:
            await _make_user(db)
        token = await _login(client)
        resp = await client.get("/api/chat/sessions", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 200
        assert resp.json()["total"] == 0

    @pytest.mark.asyncio
    async def test_send_message_full_flow(self, client):
        async with TestSession() as db:
            await _make_user(db)
        token = await _login(client)
        hdrs = {"Authorization": f"Bearer {token}"}
        sess_id = (await client.post("/api/chat/sessions", headers=hdrs)).json()["id"]
        resp = await client.post(
            f"/api/chat/sessions/{sess_id}/messages",
            json={"content": "I have a sore throat and mild fever"},
            headers=hdrs,
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["user_message"]["role"] == "user"
        assert body["assistant_message"]["role"] == "assistant"
        assert body["assistant_message"]["severity"] in ("green", "amber", "red")

    @pytest.mark.asyncio
    async def test_get_session_with_messages(self, client):
        async with TestSession() as db:
            await _make_user(db)
        token = await _login(client)
        hdrs = {"Authorization": f"Bearer {token}"}
        sess_id = (await client.post("/api/chat/sessions", headers=hdrs)).json()["id"]
        await client.post(
            f"/api/chat/sessions/{sess_id}/messages",
            json={"content": "headache for two days"},
            headers=hdrs,
        )
        resp = await client.get(f"/api/chat/sessions/{sess_id}", headers=hdrs)
        assert resp.status_code == 200
        assert len(resp.json()["messages"]) == 2

    @pytest.mark.asyncio
    async def test_title_updated_after_first_message(self, client):
        async with TestSession() as db:
            await _make_user(db)
        token = await _login(client)
        hdrs = {"Authorization": f"Bearer {token}"}
        sess_id = (await client.post("/api/chat/sessions", headers=hdrs)).json()["id"]
        await client.post(
            f"/api/chat/sessions/{sess_id}/messages",
            json={"content": "I have a persistent cough"},
            headers=hdrs,
        )
        resp = await client.get(f"/api/chat/sessions/{sess_id}", headers=hdrs)
        assert resp.json()["title"] != "New chat"

    @pytest.mark.asyncio
    async def test_list_sessions_after_message(self, client):
        async with TestSession() as db:
            await _make_user(db)
        token = await _login(client)
        hdrs = {"Authorization": f"Bearer {token}"}
        sess_id = (await client.post("/api/chat/sessions", headers=hdrs)).json()["id"]
        await client.post(
            f"/api/chat/sessions/{sess_id}/messages", json={"content": "stomach ache"}, headers=hdrs
        )
        body = (await client.get("/api/chat/sessions", headers=hdrs)).json()
        assert body["total"] == 1
        assert body["sessions"][0]["message_count"] == 2

    @pytest.mark.asyncio
    async def test_delete_session_204(self, client):
        async with TestSession() as db:
            await _make_user(db)
        token = await _login(client)
        hdrs = {"Authorization": f"Bearer {token}"}
        sess_id = (await client.post("/api/chat/sessions", headers=hdrs)).json()["id"]
        assert (
            await client.delete(f"/api/chat/sessions/{sess_id}", headers=hdrs)
        ).status_code == 204
        assert (await client.get(f"/api/chat/sessions/{sess_id}", headers=hdrs)).status_code == 404

    @pytest.mark.asyncio
    async def test_cannot_access_other_users_session(self, client):
        async with TestSession() as db:
            await _make_user(db, "owner@x.com")
            await _make_user(db, "intruder@x.com")
        owner_tok = await _login(client, "owner@x.com")
        intruder_tok = await _login(client, "intruder@x.com")
        sess_id = (
            await client.post(
                "/api/chat/sessions", headers={"Authorization": f"Bearer {owner_tok}"}
            )
        ).json()["id"]
        resp = await client.get(
            f"/api/chat/sessions/{sess_id}", headers={"Authorization": f"Bearer {intruder_tok}"}
        )
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_export_session(self, client):
        async with TestSession() as db:
            await _make_user(db)
        token = await _login(client)
        hdrs = {"Authorization": f"Bearer {token}"}
        sess_id = (await client.post("/api/chat/sessions", headers=hdrs)).json()["id"]
        await client.post(
            f"/api/chat/sessions/{sess_id}/messages",
            json={"content": "back pain for a week"},
            headers=hdrs,
        )
        export = (await client.get(f"/api/chat/sessions/{sess_id}/export", headers=hdrs)).json()
        assert export["export_format"] == "handydoctor_chat_v1"
        assert len(export["messages"]) == 2

    @pytest.mark.asyncio
    async def test_message_to_nonexistent_session(self, client):
        async with TestSession() as db:
            await _make_user(db)
        token = await _login(client)
        resp = await client.post(
            f"/api/chat/sessions/{uuid.uuid4()}/messages",
            json={"content": "hello"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_empty_content_rejected(self, client):
        async with TestSession() as db:
            await _make_user(db)
        token = await _login(client)
        hdrs = {"Authorization": f"Bearer {token}"}
        sess_id = (await client.post("/api/chat/sessions", headers=hdrs)).json()["id"]
        resp = await client.post(
            f"/api/chat/sessions/{sess_id}/messages", json={"content": ""}, headers=hdrs
        )
        assert resp.status_code == 422
