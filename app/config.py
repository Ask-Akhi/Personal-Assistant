"""Centralized config. Fail fast on missing required values."""
from __future__ import annotations

from functools import lru_cache
from typing import List

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # Core
    app_env: str = "dev"
    log_level: str = "INFO"
    timezone: str = "Asia/Kolkata"

    # DB
    database_url: str = "sqlite+aiosqlite:///./data/assistant.db"

    # Telegram
    telegram_bot_token: str = ""
    telegram_allowed_user_ids: str = ""
    admin_pin: str = ""
    pin_session_minutes: int = 15

    # Quiet hours (local time, 24h)
    quiet_hours_start: int = 22
    quiet_hours_end: int = 8
    undo_window_seconds: int = 30

    # Cost caps (USD/month)
    cost_cap_anthropic: float = 20.0
    cost_cap_openai: float = 10.0
    cost_cap_elevenlabs: float = 10.0
    cost_cap_gemini: float = 0.0   # free tier; set >0 if on pay-as-you-go
    cost_cap_groq: float = 0.0     # free tier

    # LLM provider selection
    llm_provider: str = "gemini"   # "gemini" | "groq" | "anthropic"

    # Reserved
    anthropic_api_key: str = ""
    openai_api_key: str = ""
    elevenlabs_api_key: str = ""
    gemini_api_key: str = ""
    groq_api_key: str = ""
    whatsapp_cloud_token: str = ""
    whatsapp_phone_number_id: str = ""
    whatsapp_verify_token: str = ""
    whatsapp_app_secret: str = ""
    whatsapp_group_id: str = ""   # e.g. 120363XXXXXXXXXX@g.us
    whatsapp_group_sender_url: str = ""
    whatsapp_group_sender_token: str = ""
    event_default_cutoff_hours: float = 26.0
    telegram_mirror_enabled: bool = True
    google_oauth_client_id: str = ""
    google_oauth_client_secret: str = ""

    @property
    def allowed_user_ids(self) -> List[int]:
        if not self.telegram_allowed_user_ids:
            return []
        return [
            int(x.strip())
            for x in self.telegram_allowed_user_ids.split(",")
            if x.strip()
        ]

    @field_validator("admin_pin")
    @classmethod
    def _pin_strength(cls, v: str) -> str:
        if v and (len(v) < 6 or not v.isdigit()):
            raise ValueError("ADMIN_PIN must be 6+ digits")
        return v


@lru_cache
def get_settings() -> Settings:
    return Settings()
