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

import ipaddress
import json
import os
from functools import lru_cache
from typing import Any, Dict, List, Optional, Sequence

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
    "recruitee",
    "personio",
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

# --------------------------------------------------------------------------- #
# AI token budgets — the semantics shared by the env settings, the per-user
# Settings → AI API fields and the gateway's clamp.
# --------------------------------------------------------------------------- #
#: ``0`` means **unlimited** for both budgets: no client-side clamp, the
#: provider's own per-model maximum applies. Every layer that reads a token
#: budget must treat ``0`` (and ``None``) this way — a ``value or default``
#: chain silently converts "unlimited" back into a cap.
AI_TOKEN_UNLIMITED = 0
#: Smallest *explicit* ceiling that can still produce a usable answer. Below
#: this a model cannot even emit a small JSON object, so a positive value under
#: the floor is a configuration mistake rather than a policy choice.
AI_MIN_OUTPUT_TOKENS = 64
AI_MIN_INPUT_TOKENS = 256


def ai_token_budget(value: Any) -> int:
    """Normalise a token budget: ``0``/``None``/negative → :data:`AI_TOKEN_UNLIMITED`.

    The one place that decides what an odd value means, so env, the per-user
    setting and the gateway can never disagree:

    * ``0``, ``None``, ``""`` or a negative number → ``0`` (unlimited). A
      negative value is a typo for "off", not for "1 token";
    * anything else → that positive integer.
    """
    if value is None:
        return AI_TOKEN_UNLIMITED
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return AI_TOKEN_UNLIMITED
    return parsed if parsed > 0 else AI_TOKEN_UNLIMITED


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


#: Values that mean "trust everybody" in a peer allow-list — the one shape of
#: ``TRUSTED_PROXIES`` / ``FORWARDED_ALLOW_IPS`` that turns the client IP (and
#: with it rate limits, login-failure attribution and audit rows) into
#: attacker-chosen input.
_TRUST_EVERYTHING = {"*", "any", "all"}


def _proxy_trust_entry_problem(setting: str, entry: str) -> Optional[str]:
    """One sentence saying why a peer allow-list entry is unsafe, or ``None``."""
    text = (entry or "").strip()
    if not text:
        return None
    if text.lower() in _TRUST_EVERYTHING:
        return f"{setting} entry '{text}' trusts every peer — any client can then spoof X-Forwarded-For"
    try:
        network = ipaddress.ip_network(text, strict=False)
    except ValueError:
        return f"{setting} entry '{text}' is not an IP address or CIDR"
    if network.prefixlen == 0:
        return f"{setting} entry '{text}' covers the whole address space — list the proxy's own CIDR instead"
    return None


def proxy_trust_problems(trusted_proxies: Sequence[str], forwarded_allow_ips: str) -> List[str]:
    """
    Fatal mistakes in the client-IP trust boundary.

    Both settings answer the same question — "which peers may tell us who the
    real client is?" — for two different layers (the app's own resolver and
    uvicorn's ``--proxy-headers``), so both are checked the same way: a wildcard,
    an all-covering CIDR or a value that is not an address at all means the
    deployment has handed ``X-Forwarded-For`` back to whoever is connecting.
    """
    problems: List[str] = []
    for entry in trusted_proxies or ():
        problem = _proxy_trust_entry_problem("TRUSTED_PROXIES", str(entry))
        if problem:
            problems.append(problem)
    for entry in _csv(forwarded_allow_ips):
        problem = _proxy_trust_entry_problem("FORWARDED_ALLOW_IPS", entry)
        if problem:
            problems.append(problem)
    return problems


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
    version: str = "2.3.0"
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
    #: The pre-auth login-failure window lives in this process, is keyed on the
    #: caller-supplied email address, and therefore has to be *bounded* and
    #: *tunable* rather than hardcoded: a hardcoded 8/300s cannot be tightened
    #: on an install that is being sprayed, and the key count below is the cap
    #: that stops a spray of distinct addresses from growing the map (or, in the
    #: old implementation, from wiping everybody's lockout by crossing a
    #: threshold and calling ``clear()``). See ``app/api/routers/auth.py``.
    login_throttle_window_seconds: int = 300
    login_throttle_max_keys: int = 4096

    cors_origins: List[str] = Field(default_factory=list)
    allowed_hosts: List[str] = Field(default_factory=list)
    #: Peer addresses/CIDRs allowed to speak for someone else, i.e. the reverse
    #: proxies in front of this process. ``X-Forwarded-For`` is only believed
    #: when the *peer* is on this list, and then only up to the rightmost hop
    #: that is not itself a proxy (see ``app.core.middleware.resolve_client_ip``).
    #: Empty (the default) trusts nobody: the peer address is the client, which
    #: is correct for a deployment with no proxy and *fail-closed* for one with a
    #: proxy nobody configured — every client would collapse onto the proxy's
    #: address rather than being able to spoof one.
    #: Comma separated, addresses or CIDRs: ``10.0.0.0/8,172.16.5.4``.
    trusted_proxies: List[str] = Field(default_factory=list)
    #: Mirror of uvicorn's ``--forwarded-allow-ips`` (the shipped image's CMD
    #: reads this environment variable). Uvicorn resolves ``X-Forwarded-For``
    #: itself before the app sees the request; keep this and ``TRUSTED_PROXIES``
    #: pointing at the same proxy, or one of the two layers will attribute
    #: requests to the proxy's address. ``*`` — trust every peer — is refused in
    #: production: it lets any direct client choose the IP that rate limits,
    #: login-failure attribution and audit rows are keyed on.
    forwarded_allow_ips: str = "127.0.0.1"

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
    #: uvicorn worker processes in the shipped image (the Dockerfile CMD reads
    #: ``WEB_CONCURRENCY``). **1 is the supported default**: the API process also
    #: holds the in-process pipeline worker and every per-process budget — rate
    #: limits, AI token buckets and daily budgets, per-user workflow overrides,
    #: the metrics registry. ``N`` workers means ``N`` copies of each, so limits
    #: are effectively multiplied by ``N`` and a setting saved through the API
    #: reaches one worker in ``N``. Scale with the standalone worker container
    #: (and more API replicas behind a load balancer) instead — see
    #: ``docs/DEPLOYMENT.md`` §6.
    web_concurrency: int = 1
    worker_concurrency: int = 2
    worker_poll_interval: float = 1.0
    worker_lease_seconds: int = 120
    #: Default per-item failure budget: ``job_queue.fail()`` dead-letters an
    #: item once it has recorded this many *handler failures*. Claims, pauses
    #: (AI outages) and lease expiries do not count against it — see the
    #: ``app.services.job_queue`` module docstring.
    worker_max_attempts: int = 3
    worker_max_runtime_seconds: int = 900
    #: How often each worker loop runs the stall reaper (seconds). The reaper
    #: calls :func:`job_queue.recover_stalled` on an interval, not only at
    #: boot, so a hung AI call (lease 120s, AI timeout 300s) does not leave a
    #: job stuck \"processing\" for the rest of the process lifetime.
    worker_reaper_interval_seconds: float = 60.0
    #: Safety margin for the reaper: only re-queue when the lease has been
    #: expired by more than this many seconds. This prevents cloning a merely
    #: slow job that is still inside a 300s AI call under a live worker.
    #: Default is the AI timeout (300s) — a job must be expired by >300s
    #: before it is considered truly stalled.
    worker_reaper_safety_seconds: float = 300.0
    #: How many times a job may be re-claimed due to lease expiry before it is
    #: moved to the dead-letter. A crash-looping job that never calls fail()
    #: would otherwise bounce forever.
    worker_max_reclaims: int = 5
    #: Supervisor: base delay before respawning a worker slot that died with
    #: an exception (seconds). Doubled per consecutive crash of that slot,
    #: capped by ``worker_crash_backoff_max_seconds``. 0 = respawn immediately.
    worker_respawn_backoff_seconds: float = 0.25
    #: Supervisor: upper bound on the respawn backoff above.
    worker_crash_backoff_max_seconds: float = 2.0
    #: Optional stable name for this worker process (defaults to host:pid).
    worker_id: str = ""
    #: How often the auto-mode scheduler sweeps for users whose ``auto_mode``
    #: switch is on (seconds). Each sweep enqueues whatever is *due* under the
    #: plan's cadence (see ``app/services/auto_scheduler.py``) — the interval
    #: only decides how precisely a cadence boundary is hit, never how often a
    #: workflow runs. ``0`` disables auto mode entirely (the child task is not
    #: spawned, and nothing is ever enqueued automatically); manual runs, the
    #: queue and the AI watchdog are unaffected.
    auto_scheduler_interval_seconds: float = 300.0

    # ------------------------------------------------------------------ #
    # User-action-required states (contracts/11 §2, docs/OBSERVABILITY.md)
    # ------------------------------------------------------------------ #
    #: How long a run may sit waiting on the user before it is closed out
    #: (minutes). Covers ``pipeline_jobs.status='needs_input'`` and the
    #: ``user_input_requests`` row it points at. Expiry is *not* a retry: the
    #: row is finished with a reason the user can see, and the next run starts
    #: fresh. Browser actions keep their own (shorter) TTL from the automation
    #: policy — see ``application_actions.expires_at``.
    user_action_ttl_minutes: int = Field(default=10080, ge=5, le=525600)
    #: How often the worker runs the user-action expiry sweep (seconds).
    user_action_sweep_interval_seconds: float = Field(default=900.0, ge=30.0, le=86400.0)
    #: Rows considered per sweep, per scope (bounded, oldest first).
    user_action_sweep_limit: int = Field(default=200, ge=1, le=2000)

    # ------------------------------------------------------------------ #
    # Application tracking — follow-up reminders
    # ------------------------------------------------------------------ #
    #: How far ahead a promised follow-up counts as "due" (hours). A reminder
    #: fires at the start of the window rather than exactly on the minute,
    #: because the sweep is periodic and "follow up on the 12th" should not
    #: arrive on the 13th. ``0`` means "only when the moment has passed".
    tracking_follow_up_lookahead_hours: int = Field(default=12, ge=0, le=168)
    #: Minimum minutes between two follow-up sweeps in the scheduler's
    #: maintenance pass. Independent of ``auto_scheduler_interval_seconds``:
    #: a reminder the user asked for is owed whether or not they enabled auto
    #: mode, so the pass runs for every account, not just auto-mode ones.
    tracking_follow_up_sweep_minutes: int = Field(default=15, ge=1, le=1440)
    #: Records considered per sweep (bounded, oldest due first).
    tracking_follow_up_sweep_limit: int = Field(default=200, ge=1, le=2000)

    # ------------------------------------------------------------------ #
    # Defaults used when a user has not saved their own settings
    # ------------------------------------------------------------------ #
    #: Intentionally empty. Search keywords are *extracted from the user's own
    #: resume* (Settings shows them read-only, plus an editable "extra keywords"
    #: field). Prefilling "python, backend engineer, …" silently pointed every
    #: new account at one generic search.
    default_keywords: List[str] = Field(default_factory=list)
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
    #: Hard cap on distinct rate-limit buckets kept in memory. The limiter runs
    #: before authentication, so the key set is attacker-influenced; the cap
    #: makes it a bounded LRU instead of a memory-DoS primitive (each new key
    #: past the cap evicts the least recently used one). Raise it only if
    #: ``evictions`` on ``GET /api/ops/status`` climbs while legitimate users are
    #: being throttled.
    rate_limit_max_keys: int = 8192

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
    # How long to wait for the model to finish generating (per wire attempt).
    # Heavy reasoning models legitimately take minutes on large tasks (resume
    # parsing = thinking tokens + a multi-thousand-token JSON), so this is
    # deliberately generous: 5 minutes covers even slow free-tier reasoning
    # models while still bounding every HTTP request. Configurable per user in
    # Settings → AI API (ai.timeout) and via AI_TIMEOUT; per-call overrides
    # win over both. When hit, the failure is reported honestly as `timeout`
    # (never `unreachable`) with the attempts spent and a billing caveat.
    ai_timeout: float = 300.0
    # TCP+TLS connect deadline. Kept short so a genuinely unreachable endpoint
    # (wrong base_url, DNS/TLS failure) still fails fast even though the
    # generation wait above is long. The httpx client is built as
    # Timeout(ai_timeout, connect=min(ai_connect_timeout, ai_timeout)).
    ai_connect_timeout: float = 10.0
    ai_max_retries: int = 3
    ai_backoff_base: float = 1.5
    ai_daily_token_budget: int = 0
    # Per-user OUTPUT-token ceiling (the per-request default for users who have
    # not set their own in Settings → AI API). **0 = unlimited**: the gateway
    # sends the task's own starting budget, never clamps it, and the automatic
    # escalation on truncation is free to grow past any shipped ceiling — the
    # provider's own per-model maximum is the only limit. A positive value
    # clamps every outgoing ``max_tokens`` *and* the escalation to it.
    #
    # Why 0 is the shipped default: a fixed 16000-token ceiling was the direct
    # cause of ``truncated_response`` on long job descriptions with reasoning
    # models (the thinking phase eats the budget before the JSON starts), which
    # the product then honestly reported as "AI pending" next to a preliminary
    # keyword score — a fallback, never a verdict. Unlimited is the right
    # default for a paid model; operators who want a cost cap set a number.
    ai_max_output_tokens: int = 0
    # Per-user INPUT-token ceiling (per-request default). The gateway derives a
    # character budget (≈4 chars/token) from it and caps every prompt part —
    # the old hardcoded slices (jd[:2000], json.dumps(profile)[:5000], …) all
    # obey it now. **0 = unlimited**: nothing is truncated client-side, so a
    # long JD reaches the model whole (the provider still enforces its own
    # context window, and reports it as a 400 the gateway surfaces honestly).
    ai_max_input_tokens: int = 0
    # How often the AI-availability watchdog re-probes the provider and drains
    # work that was paused during a transient outage (seconds).
    ai_watchdog_interval_seconds: float = 15.0

    # ------------------------------------------------------------------ #
    # Job sources
    # ------------------------------------------------------------------ #
    # Search is optional discovery, never an authoritative job source.
    job_search_providers: str = ""  # ordered registry ids, e.g. brave
    brave_search_api_key: str = ""
    job_search_storage_rights: bool = False  # confirm provider contract before caching
    job_search_queries_per_run: int = Field(default=3, ge=0, le=12)
    job_search_results_per_query: int = Field(default=10, ge=1, le=20)
    job_search_user_rpm: int = Field(default=6, ge=0)
    job_search_global_rpm: int = Field(default=30, ge=0)
    job_search_user_daily: int = Field(default=30, ge=0)
    job_search_global_daily: int = Field(default=1000, ge=0)
    job_search_cache_seconds: int = Field(default=3600, ge=60, le=86400)
    job_search_fetch_limit: int = Field(default=12, ge=1, le=40)
    job_search_timeout_seconds: int = Field(default=15, ge=1, le=30)
    brave_search_cost_microusd: int = Field(default=5000, ge=0)

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
    #: Relaxes the private-address check for the **HTTP client only**. Browser
    #: navigation (autofill) ignores it: a headless Chromium is the one deputy
    #: that will render an internal service and be fed vault credentials.
    outbound_allow_private: bool = False
    http_timeout: float = 20.0
    http_max_retries: int = 2
    live_scrape_timeout: float = 60.0

    # ------------------------------------------------------------------ #
    # Bounded in-process caches
    #
    # The worker is a long-lived process that sweeps thousands of *distinct*
    # URLs and hosts per day, so every one of these caches is a hard-capped LRU
    # (see app/core/lru.py) rather than a dict that only ever grows. Counters
    # (entries/evictions/hits) are on /api/ops/status.
    # ------------------------------------------------------------------ #
    #: Cached GET responses (status + headers + parsed body — never the raw
    #: httpx.Response, which pins the request, its stream and its cookies).
    http_cache_max_entries: int = 128
    #: Bodies larger than this are served but not cached: one 40 MB feed must
    #: not evict the whole cache (or dominate process memory) by itself.
    http_cache_max_body_bytes: int = 1_000_000
    #: Per-host politeness state (lock + last-call timestamp).
    http_host_state_max_entries: int = 512
    #: DNS verdicts in the SSRF guard.
    dns_cache_max_entries: int = 512
    #: Derived per-scope Fernet keys (``app.core.security._key_cache``). One
    #: entry per user vault scope, so this is bounded like the rest — and it
    #: carries a TTL so that rotating ``VAULT_KEY`` on a running process stops
    #: serving the *old* derived key within that many seconds instead of never.
    key_cache_max_entries: int = 1024
    key_cache_ttl_seconds: int = 3600

    greenhouse_board_tokens: List[str] = Field(default_factory=list)
    lever_board_tokens: List[str] = Field(default_factory=list)
    ashby_board_tokens: List[str] = Field(default_factory=list)
    workable_board_tokens: List[str] = Field(default_factory=list)
    recruitee_board_tokens: List[str] = Field(default_factory=list)
    smartrecruiters_board_tokens: List[str] = Field(default_factory=list)
    personio_board_tokens: List[str] = Field(default_factory=list)
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
    #: Web-search provider for funding scans. When resolved, scans discover
    #: funding events through a search API instead of the direct SEC EDGAR
    #: full-text endpoint (``efts.sec.gov``), whose robots.txt disallows
    #: automated access to ``/LATEST/search-index``. ``tavily`` | ``stub``
    #: (hermetic, deterministic — tests/dev) | empty (auto: ``tavily`` when
    #: ``FUNDING_SEARCH_API_KEY`` is set, else the direct sec_edgar path).
    funding_search_provider: str = ""
    funding_search_api_key: str = ""
    sec_edgar_user_agent: str = ""
    crunchbase_api_key: str = ""
    tracxn_api_key: str = ""
    funding_import_url: str = ""
    allow_synthetic_funding_data: bool = False
    funding_freshness_days: int = 45
    funding_limit: int = 18
    #: Per-provider wall-clock budget for one funding scan. A hung provider must
    #: not stall the whole radar (the scan is retried once on network errors).
    funding_provider_timeout_seconds: float = 20.0
    #: How often an *implicit* refresh (opening the Funding page) may re-scan.
    #: Deliberately independent of ``funding_freshness_days`` (which decides how
    #: old an event may be and still appear on the radar).
    funding_refresh_hours: int = 12

    # ------------------------------------------------------------------ #
    # Contacts
    # ------------------------------------------------------------------ #
    hunter_api_key: str = ""
    clearbit_api_key: str = ""
    apollo_api_key: str = ""
    contact_verify_mx: bool = True
    #: The MX probe is a blocking resolver call, so it runs on a worker thread
    #: and is capped twice: this is the resolver's own ``lifetime`` *and* the
    #: await's ceiling, and the limiter below is how many of those may be in
    #: flight at once (a discovery sweep verifying a dozen contacts must not
    #: monopolise the thread pool the rest of the app runs on).
    contact_mx_timeout_seconds: float = 5.0
    contact_mx_max_concurrent: int = 8
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
    # Browser-assisted application sessions (human-in-the-loop)
    #
    # These control *sessions*, not the legacy fire-and-forget autofill: an
    # isolated browser run per user that pauses for a human whenever a step is
    # not ours to take (login, MFA, CAPTCHA, unknown/ambiguous/legal field).
    # ------------------------------------------------------------------ #
    #: Per-user browser profile root. Profiles are never shared between users;
    #: the session's ``browser_profile_ref`` is joined under this directory and
    #: refused if it would escape it.
    browser_profile_dir: str = "./artifacts/browser_sessions"
    #: Hard session TTL. An expired session requires re-authentication — it is
    #: never extended silently.
    browser_session_ttl_minutes: int = 45
    #: Maximum live (non-terminal) sessions per user.
    browser_session_max_live: int = 2
    #: Deployment default for encrypted session persistence (opt-in per user).
    browser_session_persist: bool = False
    #: How long a minted browser-handoff link stays valid.
    browser_handoff_ttl_minutes: int = 20
    #: Screenshots are off unless a deployment says otherwise *and* the user
    #: opted in; they are never taken on login/MFA/CAPTCHA screens.
    browser_screenshots_enabled: bool = False
    #: Deployments may not retain screenshots longer than this (user requests are
    #: clamped, never trusted).
    browser_screenshot_max_retention_days: int = 30

    # ------------------------------------------------------------------ #
    # Observability
    # ------------------------------------------------------------------ #
    log_level: str = "INFO"
    log_json: bool = False
    access_log: bool = True
    metrics_enabled: bool = True
    metrics_token: str = ""
    #: When > 0, metrics are served by a separate listener on this port instead
    #: of ``GET /api/metrics`` on the public API (which then 404s). Bind by
    #: default to loopback so the port is internal unless an operator — or the
    #: compose file, for container networking — says otherwise.
    metrics_port: int = 0
    metrics_host: str = "127.0.0.1"
    #: Hard cap on distinct label combinations (series) kept per metric name.
    #: The registry is an in-process dict and a series is permanent once created,
    #: so a caller that labels on request-controlled data (a ``Host`` header, a
    #: raw path, an outbound host from a feed) would grow it forever. Callers are
    #: required to use bounded label values; this cap is the backstop that turns
    #: a mistake into evicted series (counted on ``GET /api/ops/status``) instead
    #: of an OOM.
    metrics_max_series_per_metric: int = 512

    # ------------------------------------------------------------------ #
    # Backups
    # ------------------------------------------------------------------ #
    backup_dir: str = "./backups"
    backup_keep: int = 14

    # ------------------------------------------------------------------ #
    # Billing / Monetization
    # ------------------------------------------------------------------ #
    billing_provider: str = "manual"  # manual | stripe | razorpay
    stripe_api_key: str = ""
    stripe_webhook_secret: str = ""
    stripe_price_pro_monthly: str = ""
    stripe_price_pro_plus_monthly: str = ""
    stripe_price_pro_yearly: str = ""
    stripe_price_pro_plus_yearly: str = ""
    razorpay_key_id: str = ""
    razorpay_key_secret: str = ""
    razorpay_webhook_secret: str = ""
    razorpay_plan_pro_monthly: str = ""
    razorpay_plan_pro_plus_monthly: str = ""
    billing_success_url: str = "http://localhost:5173/billing/success"
    billing_cancel_url: str = "http://localhost:5173/billing/cancel"
    trial_days: int = 7
    enable_free_tier: bool = True
    free_tier_auto_approve: bool = True

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
        "recruitee_board_tokens",
        "smartrecruiters_board_tokens",
        "personio_board_tokens",
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

    @field_validator("funding_provider", "funding_search_provider")
    @classmethod
    def _normalize_funding_provider(cls, value: str) -> str:
        """Provider ids are lowercase everywhere; ``FUNDING_PROVIDER=SEC_EDGAR``
        used to become ``unknown_provider`` and silently kill the radar."""
        return (value or "").strip().lower()

    @field_validator("billing_provider")
    @classmethod
    def _normalize_billing_provider(cls, value: str) -> str:
        """Same footgun as the funding provider: ``BILLING_PROVIDER=STRIPE`` used
        to fall through to the manual provider, silently disabling real webhooks
        while leaving the (dev-mode) manual endpoint as the billing path."""
        return (value or "manual").strip().lower()

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

    @field_validator("upload_dir", "generated_dir", "artifact_dir", "screenshot_dir", "backup_dir",
                     "browser_profile_dir")
    @classmethod
    def _absolutise_data_dir(cls, value: str) -> str:
        """Same reason as the database: data dirs must not depend on the cwd."""
        path = (value or "").strip()
        if not path or os.path.isabs(path):
            return path
        return os.path.abspath(os.path.join(BACKEND_DIR, path))

    @field_validator("ai_max_output_tokens", "ai_max_input_tokens", mode="before")
    @classmethod
    def _normalize_ai_token_budget(cls, value: Any) -> int:
        """``0`` = unlimited (provider default); a negative value means the same.

        A budget the operator meant to switch off must never come back as a
        tiny cap: ``AI_MAX_OUTPUT_TOKENS=-1`` used to mean "clamp every answer
        to 1 token", i.e. every AI call truncated. Negative now normalises to
        unlimited, the same as ``0`` and an unset value.
        """
        return ai_token_budget(value)

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
    def ai_output_unlimited(self) -> bool:
        """True when ``AI_MAX_OUTPUT_TOKENS=0`` — the provider decides the cap."""
        return self.ai_max_output_tokens <= 0

    @property
    def ai_input_unlimited(self) -> bool:
        """True when ``AI_MAX_INPUT_TOKENS=0`` — prompts are never client-truncated."""
        return self.ai_max_input_tokens <= 0

    @property
    def ai_enabled(self) -> bool:
        return bool(self.ai_api_key)

    # ------------------------------------------------------------------ #
    # Billing webhook trust boundary
    # ------------------------------------------------------------------ #
    @property
    def stripe_webhook_secret_configured(self) -> bool:
        """True when Stripe signature verification is possible for this deploy."""
        return bool((self.stripe_webhook_secret or "").strip())

    @property
    def razorpay_webhook_secret_configured(self) -> bool:
        """True when Razorpay signature verification is possible for this deploy."""
        return bool((self.razorpay_webhook_secret or "").strip())

    def billing_webhook_problems(self) -> List[str]:
        """
        Loud startup validation for the billing webhook trust boundary.

        A selected provider whose webhook secret is empty cannot verify a
        single signature, and an unverified "apply subscription changes"
        endpoint is a remote plan forge. Verification is therefore mandatory:
        ``/api/billing/webhooks/{stripe,razorpay}`` answer
        ``400 webhook_not_configured`` (and, **in production, refuse to serve
        with 503** so providers retry instead of silently marking deliveries
        successful), and ``/api/billing/webhooks/manual`` requires
        ``EMAIL_WEBHOOK_TOKEN`` in every environment. These problems are
        logged loudly at startup by ``app.main``; they do not block boot, the
        routes themselves are the boundary.
        """
        problems: List[str] = []
        provider = (self.billing_provider or "").strip().lower()
        if provider == "stripe" and not self.stripe_webhook_secret_configured:
            problems.append(
                "BILLING_PROVIDER=stripe but STRIPE_WEBHOOK_SECRET is empty — "
                "POST /api/billing/webhooks/stripe will refuse every event with "
                "webhook_not_configured (400; 503 in production); set the secret "
                "to enable payment webhooks"
            )
        if provider == "razorpay" and not self.razorpay_webhook_secret_configured:
            problems.append(
                "BILLING_PROVIDER=razorpay but RAZORPAY_WEBHOOK_SECRET is empty — "
                "POST /api/billing/webhooks/razorpay will refuse every event with "
                "webhook_not_configured (400; 503 in production); set the secret "
                "to enable payment webhooks"
            )
        return problems

    @property
    def funding_search_effective(self) -> str:
        """The resolved funding-search provider (``""`` = not configured)."""
        return resolve_funding_search_provider(self.funding_search_provider, self.funding_search_api_key)

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

    def browser_profile_path(self) -> str:
        """Root of the per-user browser profiles (isolation lives under it)."""
        return os.path.abspath(os.path.join(BACKEND_DIR, self.browser_profile_dir))

    def ensure_directories(self) -> None:
        for path in (
            self.upload_path(),
            self.generated_path(),
            self.artifact_path(),
            self.screenshot_path(),
            self.backup_path(),
            self.browser_profile_path(),
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
        # A token budget of 0 is *unlimited* and always valid. A positive value
        # under the floor is not a cost cap, it is a self-inflicted outage: no
        # model can answer a rubric/profile request in 20 output tokens, so
        # every call would come back `truncated_response`. Fail the deploy.
        if 0 < self.ai_max_output_tokens < AI_MIN_OUTPUT_TOKENS:
            problems.append(
                f"AI_MAX_OUTPUT_TOKENS must be 0 (unlimited — the provider's own maximum) or at least "
                f"{AI_MIN_OUTPUT_TOKENS} tokens, got {self.ai_max_output_tokens}"
            )
        if 0 < self.ai_max_input_tokens < AI_MIN_INPUT_TOKENS:
            problems.append(
                f"AI_MAX_INPUT_TOKENS must be 0 (unlimited — no client-side prompt truncation) or at least "
                f"{AI_MIN_INPUT_TOKENS} tokens, got {self.ai_max_input_tokens}"
            )
        if self.metrics_enabled and not (self.metrics_token or "").strip():
            problems.append(
                "METRICS_TOKEN must be set when METRICS_ENABLED=true (/api/metrics is otherwise "
                "readable by anyone) — or set METRICS_ENABLED=false and scrape nothing"
            )
        if not self.public_base_url or "localhost" in self.public_base_url:
            problems.append("PUBLIC_BASE_URL must be the public https URL of this deployment")
        # The client-IP trust boundary decides who rate limits, login-failure
        # attribution and audit rows blame. A wildcard hands that choice to any
        # client that can open a socket, so it fails the deploy like a weak
        # SECRET_KEY does.
        problems.extend(proxy_trust_problems(self.trusted_proxies, self.forwarded_allow_ips))
        if problems:
            raise ValueError("Unsafe production configuration:\n  - " + "\n  - ".join(problems))

    def warnings(self) -> List[str]:
        """Non-fatal configuration smells — logged once at startup."""
        warnings: List[str] = list(self.billing_webhook_problems())
        warnings.extend(self.scaling_warnings())
        if self.environment in PRODUCTION_ENVIRONMENTS:
            return warnings
        warnings.extend(self.secret_problems())
        if self.cors_origins and "*" in self.cors_origins:
            warnings.append("CORS_ORIGINS contains '*' — fine for development, never for production")
        return warnings

    def scaling_warnings(self) -> List[str]:
        """
        In-process state that a second worker (or replica) silently multiplies.

        The app keeps its budgets in process memory: the rate limiter, the AI
        token bucket and daily token budget, per-user workflow overrides loaded
        at boot, and the metrics registry. ``WEB_CONCURRENCY=N`` therefore means
        ``N`` independent copies — per-user limits are effectively ``N`` times
        what they say, and an override saved through the settings API reaches the
        one worker that served the request. Said loudly at startup rather than
        discovered as "the limit doesn't work".
        """
        warnings: List[str] = []
        try:
            workers = int(self.web_concurrency)
        except (TypeError, ValueError):
            workers = 1
        if workers > 1:
            warnings.append(
                f"WEB_CONCURRENCY={workers} runs {workers} API processes, and every in-process budget is "
                f"per process: rate limits and AI token/daily budgets are effectively x{workers}, a per-user "
                "AI override saved through the API reaches one worker, and /metrics covers whichever worker "
                "won the port. Use the standalone worker container (and more API replicas behind a load "
                "balancer) to scale — see docs/DEPLOYMENT.md §6."
            )
        if self.run_worker_in_api and workers > 1:
            warnings.append(
                f"RUN_WORKER_IN_API=true with WEB_CONCURRENCY={workers} starts {workers} copies of the "
                "pipeline worker in one deployment; set RUN_WORKER_IN_API=false and run `python -m app.worker`."
            )
        # Note on the proxy trust boundary: TRUSTED_PROXIES (the app's resolver)
        # and FORWARDED_ALLOW_IPS (uvicorn's --proxy-headers) are two ways to say
        # the same thing and either one is sufficient, so a "they disagree"
        # warning would be noise. A *missing* boundary cannot be detected from
        # configuration alone — it shows up at runtime as X-Forwarded-For arriving
        # from a peer nobody configured, which ``app.core.middleware`` counts
        # (``jobhunter_edge_forwarded_for_total{reason="untrusted_peer"}``) and
        # warns about once a minute.
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
            # The platform-wide token ceilings (0 = unlimited → the provider's
            # own maximum). Secret-free, and what Settings → AI API shows as
            # the default before a user sets their own per-user budget.
            "ai_max_output_tokens": self.ai_max_output_tokens,
            "ai_max_input_tokens": self.ai_max_input_tokens,
            "ai_output_unlimited": self.ai_output_unlimited,
            "ai_input_unlimited": self.ai_input_unlimited,
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
            # Secret-free funding-search status: the SPA renders it as the
            # search-provider badge on the Funding page. The key never appears.
            "funding_search_provider": self.funding_search_effective,
            "funding_search_configured": bool(self.funding_search_effective),
            "synthetic_data_allowed": self.allow_synthetic_funding_data,
            "local_data_mode": self.is_sqlite,
        }


#: Consent keys a user must accept before automation can touch their data.
REQUIRED_CONSENTS = ("terms", "privacy", "automation")


#: Funding search providers with an implementation in
#: ``app.services.funding_search`` (a registry entry, not just a plan).
FUNDING_SEARCH_PROVIDERS = ("tavily", "stub")


def resolve_funding_search_provider(provider: str, api_key: str) -> str:
    """Resolved funding-search provider id (``""`` when the search path is off).

    Single source of truth shared by :mod:`app.services.funding_search` and
    :meth:`Settings.public_settings`, so the API surface and the scan behaviour
    can never disagree about whether a search engine is configured:

    * ``stub``   — always usable (hermetic, deterministic, no network);
    * ``tavily`` — needs ``FUNDING_SEARCH_API_KEY``;
    * empty/``auto`` — ``tavily`` when a key is set, else ``""`` (direct path);
    * anything else — ``""`` (unknown values are surfaced as a hint, they never
      silently pick a provider).
    """
    wanted = (provider or "").strip().lower()
    key = (api_key or "").strip()
    if wanted == "stub":
        return "stub"
    if wanted in ("", "auto", "tavily"):
        return "tavily" if key else ""
    return ""


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
    "FUNDING_SEARCH_PROVIDERS",
    "resolve_funding_search_provider",
    "AI_TOKEN_UNLIMITED",
    "AI_MIN_OUTPUT_TOKENS",
    "AI_MIN_INPUT_TOKENS",
    "ai_token_budget",
    "BACKEND_DIR",
    "REPO_DIR",
]
