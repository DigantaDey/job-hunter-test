from pydantic_settings import BaseSettings
from typing import Optional
import os

# Repo root = two levels up from this file (backend/app/core/config.py)
BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))

class Settings(BaseSettings):
    app_name: str = "JobHunter AI"
    version: str = "1.2.0"
    secret_key: str = "dev-secret-key-change-in-production-32chars"
    database_url: str = os.getenv("DATABASE_URL", f"sqlite:///{os.path.join(BASE_DIR, 'jobhunter.db')}")
    upload_dir: str = os.getenv("UPLOAD_DIR", os.path.join(BASE_DIR, "uploads"))
    generated_dir: str = os.getenv("GENERATED_DIR", os.path.join(BASE_DIR, "generated"))
    vault_key: str = os.getenv("VAULT_KEY", "vault-encryption-key-32-chars-long!!")

    # AI defaults
    ai_base_url: str = os.getenv("AI_BASE_URL", "https://api.openai.com/v1")
    ai_api_key: str = os.getenv("AI_API_KEY", "")
    ai_model: str = os.getenv("AI_MODEL", "gpt-4o-mini")
    ai_rpm: int = int(os.getenv("AI_RPM", "60"))  # requests per minute
    ai_timeout: int = int(os.getenv("AI_TIMEOUT", "30"))

    # Scraping
    default_freshness_hours: int = 24
    default_keywords: str = "software engineer, backend, python"
    live_scraping_enabled: bool = os.getenv("LIVE_SCRAPING_ENABLED", "true").lower() in ("1", "true", "yes")
    live_scrape_timeout: int = int(os.getenv("LIVE_SCRAPE_TIMEOUT", "8"))

    # Funding radar
    funding_freshness_days: int = 45
    funding_limit: int = 18

    # CORS
    cors_origins: str = "*"

    class Config:
        env_file = ".env"

settings = Settings()
