# Privacy & Safety Review — Cross-System Data-Flow and Threat Model

**Reviewed:** 2026-09-20  
**Scope:** onboarding, discovery, matching, browser-assisted application, and
application submission flows  
**Reviewer:** automated review; findings validated against source and tests

---

## 1. Data-Flow Map

### 1.1 Resume Storage & Profile Extraction

```
Upload (PDF/DOCX) ──→ UPLOAD_DIR (content-hashed, flat basename)
                     │
                     ├─→ ResumeDocument (filepath, state, content_hash)
                     │     └─→ ResumeExtraction (append-only per attempt)
                     │           └─→ AI provider (resume text, no credentials)
                     │                 └─→ Profile (extracted fields, JSON doc)
                     │                       └─→ CandidateProfile (versioned, provenance per field)
                     │                             ├─→ ProfileFieldProvenance (confidence, evidence, sensitivity)
                     │                             └─→ ProfileFieldHistory (corrections, audit trail)
                     └─→ Generated resumes (UPLOAD_DIR/GENERATED_DIR, DOCX + rendered PDF)
```

**Sensitive fields** (per `candidate_profile.FIELD_DEFINITIONS`):
- `sensitive`: email, phone, work_authorization, sponsorship_required, salary_expectations
- `internal`: full_name, location, remote_preference, notice_period, target_roles
- `public`: roles, employers, dates, skills, tools, industries, achievements, education, certifications

**Data minimization:**
- Low-confidence values cannot silently populate required application fields — they stay in `needs_review`
- Sensitive values are masked in API responses (`value_preview = None` for `sensitive`/`restricted`)
- Evidence quotes are truncated to 500 characters
- User corrections override without deleting original evidence (history preserved)

### 1.2 Discovery & Search Query Privacy

```
User preferences (target_roles, remote) ──→ SearchPreferences.from_normalized()
                                             │
                                             ├─→ Curated role vocabulary ONLY (ROLES frozenset)
                                             │     Free-form titles are dropped, not sent raw
                                             │     Names, emails, phone, resume text NEVER used
                                             │
                                             └─→ Brave Web Search API (query, count, freshness)
                                                   └─→ Results validated against source ATS pages
                                                         before becoming job leads
```

**Privacy guarantees (enforced in code, tested):**
- `search/queries.py`: queries use ONLY a curated public role taxonomy — free-form titles,
  locations, employer names, skills, identity, and resume text are never inputs
- Unsupported preferences are omitted, not sent raw
- Search results remain discovery leads until their source pages validate them; search snippets
  are never treated as job descriptions
- Search usage is logged with a query *hash*, never the raw query text
- `test_query_privacy_fail_closed` verifies this contract

### 1.3 Matching

```
Profile data + Job description ──→ deterministic pre-rank (no AI)
                                ──→ AI rescore (top 12, Pro only)
                                      └─→ Score, reasons, matched skills
```

**Data sent to AI:** job description text + profile data (roles, skills, experience).
Credentials, passwords, and contact details are never part of a matching prompt.

### 1.4 Browser-Assisted Application Sessions

```
Start session ──→ ApplicationSession (state machine, per-user, per-job)
                  │
                  ├─→ Checkpoint (field fingerprints ONLY, never values)
                  │     value_fingerprint = sha256(value) — the original is never stored
                  │
                  ├─→ Storage state (browser cookies, encrypted per-user, opt-in)
                  │     encrypt_secret(state, "user:{id}:browser")
                  │     Purged on: expiry, cancel, erasure, reauthenticate
                  │
                  ├─→ Observations (structure, not content)
                  │     Allowed keys: url, host, title, employer, markers, fields
                  │     Dropped: html, text, content, field values
                  │
                  ├─→ Screenshots (opt-in, TTL-bounded, removed on account deletion)
                  │     NEVER_SCREENSHOT_KINDS = ("login", "mfa", "captcha")
                  │
                  └─→ Handoffs (login, MFA, CAPTCHA)
                        User completes in browser; system learns "done", not the value
                        No credential, MFA code, or CAPTCHA solution is captured
```

**Credential handling:**
- Vault passwords are decrypted in memory only for autofill typing
- The browser workflow never types a password (credential fields are skipped in `planned_values`)
- Login/MFA/CAPTCHA are handoff actions — the user performs them in the browser
- `test_no_secret_ever_reaches_the_logs` verifies credentials never appear in log output

### 1.5 Application Submission

```
Automation policy evaluation ──→ Decision (allowed/mode/blockers/limits)
                                │
                                ├─→ Consent snapshot bound to submission row
                                ├─→ At-most-once ledger (application_submissions)
                                └─→ Submission (if auto_submit explicitly opted in)
```

**Policy enforcement:**
- `auto_submit` is never enabled globally, not on any plan, not by a settings write
- Only the result of an explicit, source-specific user choice through the policy API
- EEO questions: only `never_answer` or `prefer_decline` — never silently answered
- Sensitive field policies: `ask_every_time` is the default; `use_saved_answer` is
  not available for EEO fields
- Every submission records the policy_id, policy_version, and consent snapshot

---

## 2. Threat Model

### 2.1 Credential & Secret Leakage

| Threat | Mitigation | Status |
|---|---|---|
| Password in logs | `sanitize_log_value` scrubs control chars; autofill logs selectors not values; `test_no_secret_ever_reaches_the_logs` pins this | ✅ Covered |
| API key in error messages | `safe_error_message` masks sk-*, Bearer, api_key=, token=, JWT shapes; `test_stored_errors_never_leak_provider_secrets` pins this | ✅ Covered |
| Vault password in API response | Vault list never returns `password`; reveal is a separate audited endpoint | ✅ Covered |
| MFA code captured | Browser handoff — system never sees the code; NEVER_SCREENSHOT_KINDS prevents screen capture | ✅ Covered |
| CAPTCHA solution stored | Browser handoff — system never sees the solution | ✅ Covered |
| AI prompt contains credentials | Vault credentials are never part of AI prompts; resume extraction sends only resume text | ✅ Covered |
| Search query leaks identity | Only curated role vocabulary is used; tested in `test_query_privacy_fail_closed` | ✅ Covered |
| Browser storage state leaked | Encrypted per-user; purged on expiry/cancel/erasure; never returned in API | ✅ Covered |
| Audit log contains secrets | Detail is structured metadata (action, target); callers do not pass credentials | ✅ Covered (see §4 finding) |
| Key preview in AI status | `_key_preview` shows only first 6 + last 4 chars of API key | ✅ Covered |

### 2.2 Tenant Isolation

| Threat | Mitigation | Status |
|---|---|---|
| Cross-user data access | Every query filters on `user_id`; covered by `test_auth_and_tenancy.py` | ✅ Covered |
| Vault cross-user decryption | Per-user HKDF-derived keys; `test_per_user_keys_are_isolated` | ✅ Covered |
| Session cross-tenant access | `own_session` and `get_session_by_id` filter by user_id; foreign session → 404 | ✅ Covered |
| Export of another user's data | Export is scoped to the authenticated user | ✅ Covered |
| Browser profile sandboxing | Per-user directory with resolved path checked against profile root | ✅ Covered |

### 2.3 Deletion Completeness

| Threat | Mitigation | Status |
|---|---|---|
| Orphan rows after deletion | `erasure.py` derives the plan from schema metadata; tested with FK enforcement | ✅ Covered |
| Files left on disk | `_account_files` collects resume docs, generated PDFs, screenshots; removed post-commit | ✅ Covered |
| Browser state surviving deletion | `purge_storage_state` called on erasure; `storage_state_enc` is an owned column | ✅ Covered |
| Audit trail after deletion | Audit row retained with user_id detached (legitimate-interest record) | ✅ Covered |
| Onboarding documents | `ResumeDocument.filepath` included in `_account_files` | ✅ Covered |
| Application packets | Covered by schema-derived erasure plan | ✅ Covered |
| Profile field provenance/history | Covered by schema-derived erasure plan; test seeds these tables | ✅ Covered |
| Leftover verification | `count_user_rows` post-deletion; leftovers logged as ERROR | ✅ Covered |

### 2.4 Source-Specific Policy

| Source | Automation Allowed | Status |
|---|---|---|
| LinkedIn | ❌ Gated — "silent scraping violates the ToS and gets accounts banned" | ✅ |
| Indeed | ❌ Gated — "partner-only; use a licensed aggregator" | ✅ |
| Naukri | ❌ Gated — "requires a licensed Naukri/Info Edge data agreement" | ✅ |
| Instahyre | ❌ Gated — "requires an Instahyre/Info Edge partner agreement" | ✅ |
| Greenhouse, Lever, Ashby, Workable, SmartRecruiters, Workday | ✅ Public ATS APIs; robots.txt honoured, rate-limited | ✅ |
| Remotive, Arbeitnow, Jobicy, RemoteOK, Himalayas, TheMuse, WeWorkRemotely | ✅ Public job board APIs; robots.txt honoured, cached | ✅ |
| Brave Web Search | ✅ Opt-in; API key required; query privacy enforced | ✅ |
| SEC EDGAR | ✅ Public-domain; honest User-Agent required | ✅ |

**LinkedIn terms compliance ([5](https://www.linkedin.com/legal/l/service-terms)):**
LinkedIn explicitly prohibits unauthorized automated access and data extraction. The
architecture treats this as a first-class constraint: `LinkedInSource` inherits from
`_GatedSource`, reports `available() → False`, and its `unavailable_reason` names the
specific ToS violation. No adapter can be silently enabled — gated sources require
`requires_account = True` and are `enabled_by_default = False`. A licensed partner
can add an adapter in `app/services/sources/adapters.py` without changing any other
code, but the gating is structural, not a configuration toggle.

---

## 3. User-Facing Consent Language

The four disclosures are defined in `backend/app/api/routers/account.py` and served
at `GET /api/account/disclosures`:

| Disclosure | Summary (user-facing text) |
|---|---|
| **Terms of service** | "You are responsible for the accuracy of the profile data and resumes this system submits on your behalf." |
| **Automation** | "Automation acts with your credentials on third-party job portals. Some portals prohibit automated submissions; you confirm you have the right to use each portal this way and accept that accounts may be limited." |
| **Outreach** | "Sending mail to hiring contacts must comply with anti-spam law (CAN-SPAM/GDPR). You confirm you will only contact people you may lawfully contact, that your postal address and unsubscribe link are configured, and that you will honour opt-outs." |
| **Data processing** | "Your resume, profile and credentials are stored encrypted at rest. Credentials use a per-user encryption key. You can export or delete all data at any time." |

Consent is timestamped (`{kind}_accepted_at`), revocable (`{kind}_revoked_at`), and
every accept/revoke is audit-logged. The automation policy engine binds the consent
snapshot to every submission, and re-evaluates at click time.

---

## 4. Findings & Remediation

### 4.1 HIGH — Audit detail sanitization gap (FIXED)

**Issue:** The `audit()` function in `core/audit.py` accepts an arbitrary `detail` dict
and persists it as-is. While no current caller passes credentials, there is no
structural guard against a future caller inadvertently including sensitive values
(a password from a form-mapping debug path, an API key from an error context).

**Fix:** Added `sanitize_audit_detail()` that walks the detail dict and masks values
matching known secret patterns (same patterns as `safe_error_message`). Applied at the
audit boundary so every detail dict is scrubbed regardless of caller.

### 4.2 MEDIUM — AI prompt boundary lacks explicit credential guard (FIXED)

**Issue:** The AI gateway (`ai_client.py`) sends prompts to third-party providers. While
no current workflow includes credentials in prompts, there is no explicit test or
runtime guard verifying that a prompt does not contain a vault credential, API key, or
JWT.

**Fix:** Added `_scrub_prompt_secrets()` that runs the same secret patterns over
outgoing prompts at the gateway boundary. If a match is found, the secret is masked
before the request is sent, and a metric is incremented for visibility.

### 4.3 MEDIUM — Application action payloads could carry sensitive values (FIXED)

**Issue:** `ApplicationAction.payload` is a JSON blob recording observations and fill
results. While the checkpoint uses fingerprints, the action payload could theoretically
include a field value from a user answer.

**Fix:** Added `sanitize_action_payload()` to `browser_session.py` that strips values
from fields classified as `password`, `ssn`, or `credit_card` in action payloads,
keeping only the field name, classification, and status.

### 4.4 LOW — Retention period documentation (DOCUMENTED)

**Issue:** Retention periods are operator-controlled but not summarized in one place.

**Documented periods:**
- Browser session storage state: purged on session expiry/cancel/erasure
- Screenshots: `MAX_SCREENSHOT_RETENTION_DAYS = 30` (default 7)
- Search cache: per-query TTL (default 3600s), budget rows reaped after 1 day
- Pipeline jobs: dead-letter after max attempts; no automatic purge
- Audit logs: no automatic purge (operator-controlled)
- Error logs: no automatic purge (operator-controlled)
- Onboarding sessions: persist for account lifetime; deleted with account
- Application sessions: `DEFAULT_TTL_MINUTES = 45`; terminal sessions swept lazily

### 4.5 LOW — Source adapter for Workday lacks explicit ToS reference (NOTED)

Workday's public API availability varies by tenant configuration. The adapter queries
public job board endpoints that tenants opt into exposing. No scraping is performed.
This is documented in the source list but not in COMPLIANCE.md's per-source table.
Added to the compliance documentation.

---

## 5. Limitations

1. **AI provider trust:** Resume text is sent to the configured AI provider for
   extraction. This is a necessary part of the extraction workflow and requires the
   data_processing consent. The provider sees the resume content but never credentials
   or vault data.

2. **Browser session cookies:** Opt-in browser persistence stores encrypted session
   cookies for third-party portals. These are encrypted per-user and purged on expiry,
   but the operator should understand that the server holds decryptable portal session
   state while a session is live.

3. **Search provider visibility:** Brave Web Search (when enabled) sees the query text.
   The query privacy module ensures only curated role vocabulary is sent, but the
   provider can correlate queries over time.

4. **Funding data quality:** SEC EDGAR Form D data is public-domain. Crunchbase/Tracxn
   require licensed keys. Synthetic demo data is opt-in and labelled `DEMO DATA`.

5. **Contact discovery:** Hunter/Apollo APIs see the domain and name being queried.
   Heuristic fallbacks are always marked `verified=false` and never invent a personal
   address.

---

## 6. Test Coverage Summary

| Area | Test File(s) |
|---|---|
| Log sanitization | `test_backend_robustness.py`, `test_onboarding_flow.py` |
| Secret redaction in errors | `test_onboarding_flow.py::test_stored_errors_never_leak_provider_secrets` |
| Credential isolation | `test_vault_and_security.py::test_per_user_keys_are_isolated` |
| No secrets in autofill logs | `test_autofill_navigation_policy.py::test_no_secret_ever_reaches_the_logs` |
| Tenant isolation | `test_auth_and_tenancy.py`, `test_vault_and_security.py` |
| Deletion completeness | `test_deletion_integrity.py` (every owned table seeded, FK enforced) |
| Search query privacy | `test_search_discovery.py::test_query_privacy_fail_closed` |
| Consent enforcement | `test_automation_policy_contract.py`, `test_settings_and_compliance.py` |
| Export completeness | `test_deletion_integrity.py::test_export_pages_every_collection_and_returns_it_all` |
| Prompt redaction | `test_privacy_redaction.py` (NEW) |
| Audit detail sanitization | `test_privacy_redaction.py` (NEW) |
| Action payload sanitization | `test_privacy_redaction.py` (NEW) |
| Browser session privacy | `test_browser_sessions.py` |
