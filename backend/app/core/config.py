"""
Application configuration.

Single source of truth for every knob in JobHunter AI. Values come from the
environment (and an optional ``.env`` file); every setting has a safe default so
a bare checkout boots in local development mode with no secrets configured.

Production is *not* permissive: :meth:`Settings.validate_for_runtime` fails fast
on placeholder secrets, wildcard CORS, disabled auth, missing compliance
settings for outbound email, and (by default) SQLite as the primary database.
"""

from __future__ import annotations

import json
import os
from functools import lru_cache
from typing import Any, Dict, List

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# --------------------------------------------------------------------------- #
# Paths & constants
# --------------------------------------------------------------------------- #
BACKEND_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
REPO_DIR = os.path.abspath(os.path.join(BACKEND_DIR, ".."))

DEV_SECRET = "dev-only-change-me"
WEAK_MARKERS = (
    "change-me",
    "changeme",
    "dev-only",
    "dev-secret",
    "placeholder",
    "example",
    "secret123",
    "your-secret",
    "todo",
)

PRODUCTION_ENVIRONMENTS = {"production", "prod", "staging"}

#: Job sources that can run without credentials. Boards are keyed by the
#: identifier used in the API and in ``ENABLED_SOURCES``.
LIVE_SOURCES = (
    "greenhouse",
    "lever",
    "ashby",
    "smartrecruiters",
    "workable",
    "arbeitnow",
    "himalayas",
    "jobicy",
    "remoteok",
    "remotive",
    "themuse",
    "weworkremotely",
    "workday",
)

#: Sources that are implemented but need partner credentials and/or are
#: explicitly rate limited by their terms. Disabled unless keys are provided.
CREDENTIALED_SOURCES = ("adzuna", "jooble", "usajobs", "linkedin", "naukri", "indeed", "instahyre")


def _maybe_json(value: str) -> Any:
    """
    Return the decoded object when ``value`` is a JSON array/object, else ``None``.

    Settings that hold lists are documented as comma separated. ``pydantic-settings``
    would otherwise try ``json.loads`` on them first (and crash on ``CORS_ORIGINS=``),
    so :class:`Settings` disables that decoding and parses everything itself — while
    still accepting JSON for anyone who already wrote it that way.
    """
    text = (value or "").strip()
    if text[:1] in ("[", "{"):
        try:
            return json.loads(text)
        except ValueError:
            return None
    return None


def _csv(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return [str(v).strip() for v in value if str(v).strip()]
    if isinstance(value, (dict, int, float, bool)):
        return [str(value).strip()]
    text = str(value).strip()
    if not text:
        return []
    decoded = _maybe_json(text)
    if decoded is not None:
        return _csv(decoded)
    return [part.strip() for part in text.replace(";", ",").split(",") if part.strip()]


def _csv_lower(value: Any) -> List[str]:
    return [v.lower() for v in _csv(value)]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=os.path.join(REPO_DIR, ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
        # Normalise the defaults too (relative SQLite paths / data directories),
        # not just values that arrive from the environment.
        validate_default=True,
        # List settings are documented (and used) as comma separated values, so
        # pydantic-settings must hand us the raw string instead of json.loads-ing
        # it — that raised SettingsError for every empty or CSV list value.
        # Our own ``mode="before"`` validators do the parsing (and still accept JSON).
        enable_decoding=False,
    )

    # ------------------------------------------------------------------ #
    # Application
    # ------------------------------------------------------------------ #
    app_name: str = "JobHunter AI"
    version: str = "2.0.0"
    environment: str = "development"
    debug: bool = False
    public_base_url: str = "http://localhost:8000"
    api_prefix: str = "/api"

    # ------------------------------------------------------------------ #
    # Security
    # ------------------------------------------------------------------ #
    secret_key: str = DEV_SECRET
    encryption_key: str = ""
    #: Legacy alias kept so older deployments keep working after an upgrade.
    vault_key: str = ""

    jwt_algorithm: str = "HS256"
    access_token_minutes: int = 30
    refresh_token_days: int = 30
    auth_required: bool = True
    allow_registration: bool = False
    password_min_length: int = 10
    max_login_attempts: int = 8
    login_lockout_seconds: int = 900

    cors_origins: List[str] = Field(default_factory=list)
    allowed_hosts: List[str] = Field(default_factory=list)
    trusted_proxies: List[str] = Field(default_factory=list)

    # ------------------------------------------------------------------ #
    # Database
    # ------------------------------------------------------------------ #
    database_url: str = "sqlite:///./jobhunter.db"
    allow_sqlite_in_prod: bool = False
    auto_migrate: bool = True
    db_pool_size: int = 10
    db_max_overflow: int = 20
    db_pool_recycle: int = 1800

    # ------------------------------------------------------------------ #
    # Workers / queue
    # ------------------------------------------------------------------ #
    run_worker_in_api: bool = True
    worker_concurrency: int = 2
    worker_poll_interval: float = 1.0
    worker_lease_seconds: int = 120
    worker_max_attempts: int = 3
    worker_max_runtime_seconds: int = 900
    #: Optional stable name for this worker process (defaults to host:pid).
    worker_id: str = ""

    # ------------------------------------------------------------------ #
    # Defaults used when a user has not saved their own settings
    # ------------------------------------------------------------------ #
    default_keywords: List[str] = Field(
        default_factory=lambda: [
            "python", "backend engineer", "data engineer", "machine learning",
            "devops", "full stack", "platform engineer", "site reliability",
        ]
    )
    default_freshness_hours: int = 72
    discovery_cache_seconds: int = 900
    ai_max_concurrency: int = 4
    ai_breaker_failures: int = 5
    ai_breaker_cooldown_seconds: int = 120

    # ------------------------------------------------------------------ #
    # Rate limiting
    # ------------------------------------------------------------------ #
    api_rate_limit_per_minute: int = 600
    auth_rate_limit_per_minute: int = 20

    # ------------------------------------------------------------------ #
    # Files
    # ------------------------------------------------------------------ #
    upload_dir: str = "./uploads"
    generated_dir: str = "./generated"
    artifact_dir: str = "./artifacts"
    max_upload_mb: int = 15

    # ------------------------------------------------------------------ #
    # AI provider
    # ------------------------------------------------------------------ #
    ai_base_url: str = "https://api.openai.com/v1"
    ai_api_key: str = ""
    ai_model: str = "gpt-4o-mini"
    ai_rpm: int = 60
    ai_timeout: float = 30.0
    ai_max_retries: int = 3
    ai_backoff_base: float = 1.5
    ai_daily_token_budget: int = 0
    ai_max_output_tokens: int = 1200

    # ------------------------------------------------------------------ #
    # Job sources
    # ------------------------------------------------------------------ #
    live_scraping_enabled: bool = True
    respect_robots_txt: bool = True
    per_host_min_interval_seconds: float = 1.0
    max_concurrent_fetches: int = 4
    max_ats_boards_per_run: int = 12
    include_demo_pool: bool = False
    enabled_sources: List[str] = Field(default_factory=list)
    http_user_agent: str = "JobHunterAI/2.0 (+https://github.com/jobhunter-ai)"
    #: SSRF policy (see app/services/net_guard.py)
    outbound_allowed_hosts: List[str] = Field(default_factory=list)
    outbound_allow_private: bool = False
    http_timeout: float = 20.0
    http_max_retries: int = 2
    live_scrape_timeout: float = 60.0

    greenhouse_board_tokens: List[str] = Field(default_factory=list)
    lever_board_tokens: List[str] = Field(default_factory=list)
    ashby_board_tokens: List[str] = Field(default_factory=list)
    workable_board_tokens: List[str] = Field(default_factory=list)
    smartrecruiters_board_tokens: List[str] = Field(default_factory=list)
    #: "host|tenant|site" triples for the Workday union API.
    workday_board_tokens: List[str] = Field(default_factory=list)

    adzuna_app_id: str = ""
    adzuna_app_key: str = ""
    adzuna_country: str = "in"
    jooble_api_key: str = ""
    usajobs_api_key: str = ""
    usajobs_email: str = ""

    # ------------------------------------------------------------------ #
    # Funding
    # ------------------------------------------------------------------ #
    funding_provider: str = "sec_edgar"
    sec_edgar_user_agent: str = ""
    crunchbase_api_key: str = ""
    tracxn_api_key: str = ""
    funding_import_url: str = ""
    allow_synthetic_funding_data: bool = False
    funding_freshness_days: int = 45
    funding_limit: int = 18

    # ------------------------------------------------------------------ #
    # Contacts
    # ------------------------------------------------------------------ #
    hunter_api_key: str = ""
    clearbit_api_key: str = ""
    apollo_api_key: str = ""
    contact_verify_mx: bool = True
    contact_max_candidates: int = 5

    # ------------------------------------------------------------------ #
    # Email / outreach
    # ------------------------------------------------------------------ #
    email_sending_enabled: bool = False
    email_dry_run: bool = True
    email_daily_limit: int = 40
    email_batch_size: int = 10
    email_smtp_host: str = ""
    email_smtp_port: int = 587
    email_smtp_username: str = ""
    email_smtp_password: str = ""
    email_smtp_use_tls: bool = True
    email_from_name: str = ""
    email_postal_address: str = ""
    email_unsubscribe_base_url: str = ""
    email_tracking_enabled: bool = True
    email_webhook_token: str = ""

    # ------------------------------------------------------------------ #
    # Browser automation
    # ------------------------------------------------------------------ #
    autofill_enabled: bool = False
    autofill_dry_run: bool = True
    autofill_allow_submit: bool = False
    autofill_headless: bool = True
    autofill_timeout_ms: int = 45000
    screenshot_dir: str = "./artifacts/screenshots"

    # ------------------------------------------------------------------ #
    # Observability
    # ------------------------------------------------------------------ #
    log_level: str = "INFO"
    log_json: bool = False
    access_log: bool = True
    metrics_enabled: bool = True
    metrics_token: str = ""

    # ------------------------------------------------------------------ #
    # Backups
    # ------------------------------------------------------------------ #
    backup_dir: str = "./backups"
    backup_keep: int = 14

    # ==================================================================== #
    # Validators
    # ==================================================================== #
    @model_validator(mode="before")
    @classmethod
    def _strip_inline_comment_values(cls, values: Any) -> Any:
        """
        ``SECRET_KEY=   # comment`` is a real-world footgun: dotenv reads the
        comment text as the value. Treat comment-only values as unset.
        """
        if isinstance(values, dict):
            for key, value in list(values.items()):
                if isinstance(value, str) and value.lstrip().startswith("#"):
                    values[key] = ""
        return values

    @field_validator("outbound_allowed_hosts", mode="before")
    @classmethod
    def _split_outbound(cls, value: Any) -> List[str]:
        return _csv_lower(value)

    @field_validator("cors_origins", "allowed_hosts", "trusted_proxies", mode="before")
    @classmethod
    def _split_lists(cls, value: Any) -> List[str]:
        return _csv(value)

    @field_validator(
        "enabled_sources",
        "greenhouse_board_tokens",
        "lever_board_tokens",
        "ashby_board_tokens",
        "workable_board_tokens",
        "smartrecruiters_board_tokens",
        mode="before",
    )
    @classmethod
    def _split_lower_lists(cls, value: Any) -> List[str]:
        return _csv_lower(value)

    @field_validator("workday_board_tokens", mode="before")
    @classmethod
    def _split_workday(cls, value: Any) -> List[str]:
        return _csv(value)

    @field_validator("default_keywords", mode="before")
    @classmethod
    def _split_keywords(cls, value: Any) -> List[str]:
        return _csv(value)

    @field_validator("environment")
    @classmethod
    def _normalize_environment(cls, value: str) -> str:
        return (value or "development").strip().lower()

    @field_validator("log_level")
    @classmethod
    def _normalize_log_level(cls, value: str) -> str:
        level = (value or "INFO").strip().upper()
        return level if level in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"} else "INFO"

    @field_validator("database_url")
    @classmethod
    def _normalize_database_url(cls, value: str) -> str:
        url = (value or "").strip()
        if url.startswith("postgres://"):  # Heroku/Render style alias
            url = url.replace("postgres://", "postgresql+psycopg2://", 1)
        elif url.startswith("postgresql://") and "+psycopg2" not in url:
            url = url.replace("postgresql://", "postgresql+psycopg2://", 1)
        elif url.startswith("sqlite") and "://" in url:
            # A relative SQLite path resolves against the *working directory*,
            # so `./run.sh` (cwd=backend), `python -m app.worker` and the CLI
            # scripts would each open a different database. Pin it next to the
            # backend package instead. Four slashes (`sqlite:////abs/path`) and
            # `:memory:` are already unambiguous and are left alone.
            scheme, _, rest = url.partition("://")
            candidate = rest.lstrip("/")
            if (
                candidate
                and not rest.startswith("//")          # already absolute (sqlite:////abs/path)
                and not candidate.startswith(":memory:")
                and not os.path.isabs(candidate)
            ):
                url = f"{scheme}:///{os.path.abspath(os.path.join(BACKEND_DIR, candidate))}"
        return url or "sqlite:///./jobhunter.db"

    @field_validator("upload_dir", "generated_dir", "artifact_dir", "screenshot_dir", "backup_dir")
    @classmethod
    def _absolutise_data_dir(cls, value: str) -> str:
        """Same reason as the database: data dirs must not depend on the cwd."""
        path = (value or "").strip()
        if not path or os.path.isabs(path):
            return path
        return os.path.abspath(os.path.join(BACKEND_DIR, path))

    @model_validator(mode="after")
    def _reject_unsafe_production(self) -> "Settings":
        """Constructing a production Settings with unsafe values fails loudly."""
        self.validate_for_runtime()
        return self

    # ==================================================================== #
    # Derived properties
    # ==================================================================== #
    @property
    def is_production(self) -> bool:
        return self.environment in PRODUCTION_ENVIRONMENTS

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")

    @property
    def encryption_key_effective(self) -> str:
        """Master key used to derive per-user vault keys."""
        return self.encryption_key or self.vault_key or self.secret_key

    @property
    def jwt_secret(self) -> str:
        return self.secret_key or DEV_SECRET

    @property
    def cors_origin_list(self) -> List[str]:
        """Origins handed to CORSMiddleware (``*`` when unset in development)."""
        return self.cors_origins or ["*"]

    @property
    def allowed_host_list(self) -> List[str]:
        """Hosts handed to TrustedHostMiddleware (``*`` when unset)."""
        return self.allowed_hosts or ["*"]

    @property
    def email_real_sending(self) -> bool:
        """True only when outbound mail is fully configured and not dry-run."""
        return self.real_sending_enabled

    @property
    def smtp_configured(self) -> bool:
        return bool(self.email_smtp_host)

    @property
    def compliance_ready_for_sending(self) -> bool:
        """CAN-SPAM requires a physical address and a working opt-out link."""
        return bool(self.email_postal_address and self.email_unsubscribe_base_url)

    @property
    def real_sending_enabled(self) -> bool:
        return (
            self.email_sending_enabled
            and not self.email_dry_run
            and self.smtp_configured
            and self.compliance_ready_for_sending
        )

    @property
    def ai_enabled(self) -> bool:
        return bool(self.ai_api_key)

    @property
    def autofill_runtime_ready(self) -> bool:
        if not self.autofill_enabled:
            return False
        try:  # pragma: no cover - depends on optional extra
            import playwright  # noqa: F401
        except Exception:
            return False
        return True

    @property
    def enabled_source_names(self) -> List[str]:
        if self.enabled_sources:
            return list(self.enabled_sources)
        names = list(LIVE_SOURCES)
        if self.include_demo_pool:
            names.append("demo")
        return names

    def upload_path(self) -> str:
        return os.path.abspath(os.path.join(BACKEND_DIR, self.upload_dir))

    def generated_path(self) -> str:
        return os.path.abspath(os.path.join(BACKEND_DIR, self.generated_dir))

    def artifact_path(self) -> str:
        return os.path.abspath(os.path.join(BACKEND_DIR, self.artifact_dir))

    def screenshot_path(self) -> str:
        return os.path.abspath(os.path.join(BACKEND_DIR, self.screenshot_dir))

    def backup_path(self) -> str:
        return os.path.abspath(os.path.join(BACKEND_DIR, self.backup_dir))

    def ensure_directories(self) -> None:
        for path in (
            self.upload_path(),
            self.generated_path(),
            self.artifact_path(),
            self.screenshot_path(),
            self.backup_path(),
        ):
            os.makedirs(path, exist_ok=True)

    # ==================================================================== #
    # Validation
    # ==================================================================== #
    def secret_problems(self) -> List[str]:
        """Non-fatal hygiene problems (surfaced at startup and in /api/meta)."""
        problems: List[str] = []
        secret = (self.secret_key or "").strip()
        if not secret or secret == DEV_SECRET or any(m in secret.lower() for m in WEAK_MARKERS):
            problems.append("SECRET_KEY is unset or a well-known default")
        elif len(secret) < 32:
            problems.append("SECRET_KEY should be at least 32 characters")
        key = (self.encryption_key or self.vault_key or "").strip()
        if key and any(m in key.lower() for m in WEAK_MARKERS):
            problems.append("ENCRYPTION_KEY looks like a placeholder")
        elif key and len(key) < 32:
            problems.append("ENCRYPTION_KEY should be at least 32 characters")
        elif not key:
            problems.append("ENCRYPTION_KEY is unset — falling back to SECRET_KEY")
        if self.email_sending_enabled and not self.compliance_ready_for_sending:
            problems.append("EMAIL_POSTAL_ADDRESS / EMAIL_UNSUBSCRIBE_BASE_URL missing while sending is enabled")
        return problems

    def validate_for_runtime(self) -> None:
        """
        Fail fast on unsafe production configuration. Raises ``ValueError`` with
        every problem listed at once so a single deploy fixes them all.
        """
        if not self.is_production:
            return
        problems: List[str] = []
        secret = (self.secret_key or "").strip()
        if not secret:
            problems.append("SECRET_KEY must be set to a random 32+ character value")
        elif secret == DEV_SECRET or any(m in secret.lower() for m in WEAK_MARKERS):
            problems.append("SECRET_KEY is a placeholder value — generate a real secret")
        elif len(secret) < 32:
            problems.append("SECRET_KEY must be at least 32 characters")
        key = (self.encryption_key or self.vault_key or "").strip()
        if not key:
            problems.append("ENCRYPTION_KEY must be set (vault encryption is not optional in production)")
        elif any(m in key.lower() for m in WEAK_MARKERS):
            problems.append("ENCRYPTION_KEY is a placeholder value")
        elif len(key) < 32:
            problems.append("ENCRYPTION_KEY must be at least 32 characters")
        if not self.cors_origins:
            problems.append("CORS_ORIGINS must list the browser origin(s) that serve the SPA")
        elif "*" in self.cors_origins:
            problems.append("CORS_ORIGINS must list explicit origins (wildcard is unsafe with credentials)")
        if not self.auth_required:
            problems.append("AUTH_REQUIRED must not be disabled in production")
        if self.debug:
            problems.append("DEBUG must be false in production")
        if self.is_sqlite and not self.allow_sqlite_in_prod:
            problems.append(
                "SQLite is not supported for production — set DATABASE_URL to PostgreSQL "
                "(or set ALLOW_SQLITE_IN_PROD=true to accept a single-host deployment)"
            )
        if self.email_sending_enabled and not self.email_dry_run:
            if not self.smtp_configured:
                problems.append("EMAIL_SMTP_HOST is required when real email sending is enabled")
            if not self.compliance_ready_for_sending:
                problems.append(
                    "EMAIL_POSTAL_ADDRESS and EMAIL_UNSUBSCRIBE_BASE_URL are required to send commercial email"
                )
        if self.autofill_allow_submit and not self.autofill_enabled:
            problems.append("AUTOFILL_ALLOW_SUBMIT requires AUTOFILL_ENABLED=true")
        if not self.public_base_url or "localhost" in self.public_base_url:
            problems.append("PUBLIC_BASE_URL must be the public https URL of this deployment")
        if problems:
            raise ValueError("Unsafe production configuration:\n  - " + "\n  - ".join(problems))

    def warnings(self) -> List[str]:
        """Non-fatal configuration smells — logged once at startup."""
        warnings: List[str] = []
        if self.environment in PRODUCTION_ENVIRONMENTS:
            return warnings
        warnings.extend(self.secret_problems())
        if self.cors_origins and "*" in self.cors_origins:
            warnings.append("CORS_ORIGINS contains '*' — fine for development, never for production")
        return warnings

    def public_settings(self) -> Dict[str, Any]:
        """Secret-free snapshot safe to expose to the SPA (``/api/meta``)."""
        return {
            "app_name": self.app_name,
            "version": self.version,
            "environment": self.environment,
            "live_scraping_enabled": self.live_scraping_enabled,
            "ai_model": self.ai_model,
            "ai_configured": self.ai_enabled,
            "email_sending_enabled": self.email_sending_enabled,
            "email_dry_run": self.email_dry_run,
            "email_configured": self.smtp_configured,
            "email_compliance_ready": self.compliance_ready_for_sending,
            "autofill_enabled": self.autofill_enabled,
            "autofill_dry_run": self.autofill_dry_run,
            "autofill_allow_submit": self.autofill_allow_submit,
            "registration_open": self.allow_registration,
            "required_consents": list(REQUIRED_CONSENTS),
            "sources_live": list(LIVE_SOURCES),
            "sources_credentialed": list(CREDENTIALED_SOURCES),
            "funding_provider": self.funding_provider,
            "synthetic_data_allowed": self.allow_synthetic_funding_data,
            "local_data_mode": self.is_sqlite,
        }


#: Consent keys a user must accept before automation can touch their data.
REQUIRED_CONSENTS = ("terms", "privacy", "automation")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached settings singleton (tests call ``get_settings.cache_clear()``)."""
    settings = Settings()
    settings.validate_for_runtime()
    return settings


settings = get_settings()

__all__ = [
    "Settings",
    "settings",
    "get_settings",
    "LIVE_SOURCES",
    "CREDENTIALED_SOURCES",
    "REQUIRED_CONSENTS",
    "BACKEND_DIR",
    "REPO_DIR",
]
