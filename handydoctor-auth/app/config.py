"""
app/config.py — Centralised settings loaded from .env via pydantic-settings.
All downstream modules import `settings` from here.
"""

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # ── Database ──────────────────────────────────────────────────────────
    database_url: str

    # ── Redis ─────────────────────────────────────────────────────────────
    redis_url: str = "redis://localhost:6379/0"

    # ── JWT ───────────────────────────────────────────────────────────────
    jwt_private_key_path: str = "private.pem"
    jwt_public_key_path: str = "public.pem"
    jwt_algorithm: str = "RS256"
    access_token_expire_minutes: int = 15
    refresh_token_expire_days: int = 7

    # ── Google OAuth ──────────────────────────────────────────────────────
    google_client_id: str = ""
    google_client_secret: str = ""
    google_redirect_uri: str = "http://localhost:8000/api/accounts/oauth/google/callback"

    # ── Email ─────────────────────────────────────────────────────────────
    resend_api_key: str = ""
    email_from: str = "noreply@handydoctor.app"
    email_from_name: str = "HandyDoctor"

    # ── Anthropic (AI chat) ───────────────────────────────────────────────
    anthropic_api_key: str = ""
    anthropic_model: str = "claude-haiku-4-5-20251001"
    chat_max_tokens: int = 600

    # ── App ───────────────────────────────────────────────────────────────
    app_name: str = "HandyDoctor"
    frontend_url: str = "http://localhost:3000"
    email_verification_expire_hours: int = 24
    account_deletion_grace_days: int = 14
    secret_key: str = "change-me"
    allowed_origins: str = "http://localhost:3000"
    trust_proxy: bool = False

    # ── Rate-limit constants ──────────────────────────────────────────────
    login_max_attempts: int = 5
    login_lockout_minutes: int = 15
    openai_model: str = "gpt-4o-mini"
    login_lockout_minutes: int = 15

    @property
    def jwt_private_key(self) -> str:
        return Path(self.jwt_private_key_path).read_text()

    @property
    def jwt_public_key(self) -> str:
        return Path(self.jwt_public_key_path).read_text()

    @property
    def cors_origins(self) -> list[str]:
        return [o.strip() for o in self.allowed_origins.split(",")]


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
