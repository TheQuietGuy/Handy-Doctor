"""
main.py — HandyDoctor FastAPI application entry point.

Run with:
    uvicorn main:app --reload --host 0.0.0.0 --port 8000
"""

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.chat.router import router as chat_router
from app.config import settings
from app.database import close_redis, engine, get_redis
from app.gdpr.scheduler import create_scheduler, start_scheduler, stop_scheduler
from app.middleware.rate_limit import RateLimitMiddleware
from app.models import Base
from app.logging_config import configure_logging
from app.routers import accounts, admin


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging()
    # ── Startup ───────────────────────────────────────────────────────────
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    # Store Redis on app.state so RateLimitMiddleware can reach it
    app.state.redis = await get_redis()

    create_scheduler()
    start_scheduler()

    yield

    # ── Shutdown ──────────────────────────────────────────────────────────
    stop_scheduler()
    await close_redis()
    await engine.dispose()


app = FastAPI(
    title="HandyDoctor — Account Management API",
    version="1.0.0",
    description=(
        "RS256 JWT authentication, Google OAuth 2.0, bcrypt (cost 12), "
        "Redis-backed sliding-window rate limiting, and GDPR-compliant account deletion."
    ),
    docs_url="/docs",
    redoc_url="/redoc",
    lifespan=lifespan,
)

# ── Middleware ────────────────────────────────────────────────────────────────
# Starlette processes middleware in reverse registration order (last-in = outermost).
# We want: CORS (outermost) → RateLimit → Route handler (innermost).
# So we register RateLimit first, then CORS.
app.add_middleware(RateLimitMiddleware)  # reads redis from app.state at request time
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Routers ───────────────────────────────────────────────────────────────────
app.include_router(accounts.router)
app.include_router(admin.router)
app.include_router(chat_router)


@app.get("/health", tags=["health"])
async def health() -> dict:
    return {"status": "ok", "service": settings.app_name}
