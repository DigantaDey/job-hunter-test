from pydantic_settings import BaseSettings
from typing import Optional, Dict, Any
import os

class Settings(BaseSettings):
    app_name: str = "JobHunter AI"
    version: str = "1.0.0"
    secret_key: str = "dev-secret-key-change-in-production-32chars"
    database_url: str = "sqlite:////home/user/job-hunter-test/jobhunter.db"
    upload_dir: str = "/home/user/job-hunter-test/uploads"
    generated_dir: str = "/home/user/job-hunter-test/generated"
    vault_key: str = "vault-encryption-key-32-chars-long!!"

    # AI defaults
    ai_base_url: str = os.getenv("AI_BASE_URL", "https://api.openai.com/v1")
    ai_api_key: str = os.getenv("AI_API_KEY", "")
    ai_model: str = os.getenv("AI_MODEL", "gpt-4o-mini")
    ai_rpm: int = 60  # requests per minute
    ai_timeout: int = 30

    # Scraping
    default_freshness_hours: int = 24
    default_keywords: str = "software engineer, backend, python"

    # CORS
    cors_origins: str = "*"

    class Config:
        env_file = ".env"

settings = Settings()
