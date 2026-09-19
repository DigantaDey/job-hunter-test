# 10 — User automation policy schema

**Deliverable 8.** One object that answers *"what is this system allowed to do for
this user, right now, for this workflow?"* — and can prove it afterwards.

Today the answer is assembled at run time from four places:
`settings` rows (`application.allow_auto_submit`, `automation.auto_mode`,
`compliance.rate_limit_per_day`, `workflows.ai_for_*`), `User.consents`,
`app/core/entitlements.py` (plan capabilities + monthly counters), and
`app/core/config.py` (server flags `AUTOFILL_ENABLED`, `AUTOFILL_DRY_RUN`,
`AUTOFILL_ALLOW_SUBMIT`). Every one of those checks is real and enforced — the
problem is that nothing records the *combination* that was in force when an
application was submitted, and a user cannot see or edit it as one thing.

This contract keeps all four as inputs and adds a persisted, versioned policy
plus a recorded **decision**.

---

## 1. Inputs, in precedence order

```
effective = resolve(
  server_config      (what this deployment allows at all)      ← ceiling
  plan_entitlements  (what this tenant's plan includes)         ← ceiling
  consent_state      (what the user has authorised)             ← gate
  policy_rows        (what the user asked for)                  ← choice
)
```

**Most restrictive wins.** A user cannot raise a ceiling; a plan cannot grant a
consent; a consent cannot be substituted by a setting. The resolution is a pure
function (§5) so the API, the worker and the scheduler all get the same answer —
the reason `auto_scheduler.overview()` is the only place the Settings card reads
from today.

---

## 2. `automation_policies`

One row per `(user_id, scope, scope_key, workflow)`. A user always has exactly
one `global`/`*` row per workflow (created with the account, defaults from the
plan); narrower rows override it.

| Column | Type | Null | Meaning |
|---|---|---|---|
| `id` | PK | no | |
| `user_id` | FK, index | no | tenant |
| `scope` | String(16) | no | `AUTOMATION_SCOPES`: `global` \| `persona` \| `workflow` \| `company` \| `portal` |
| `scope_key` | String(200) | no | the persona id, workflow name, `company_name_normalized`, or portal domain; `*` for `global` |
| `workflow` | String(28) | no | `AUTOMATION_WORKFLOWS` |
| `enabled` | Boolean | no | master switch for this row |
| `mode` | String(16) | no | `AUTOMATION_MODES`: `off` \| `suggest` \| `prepare` \| `auto_submit` |
| `mode_chosen_at` | DateTime | yes | `null` = inherited default, not an explicit choice (the onboarding gate needs the difference) |
| `min_match_score` | Float | yes | floor for acting; `null` = no floor |
| `max_runs_per_day` | Integer | yes | `null` = plan default |
| `max_runs_per_month` | Integer | yes | |
| `require_review_before_submit` | Boolean | no | default `true` |
| `require_resume_approval` | Boolean | no | default `true` (the shipped `general.require_resume_approval`) |
| `allowed_portals` | JSON | no | `[]` = any; otherwise a domain allow-list (intersected with the autofill domain policy in `apply_flow.company_domains`) |
| `blocked_portals` | JSON | no | |
| `allowed_companies` / `blocked_companies` | JSON | no | `company_name_normalized` values |
| `blocked_keywords` | JSON | no | a posting whose title/JD matches is skipped |
| `allowed_locations` | JSON | no | `{country_code, region, city, remote_policy}` filters |
| `require_remote` / `require_onsite` | Boolean | no | |
| `compensation_floor` | JSON | no | `{currency, period, amount}` — skip postings below it **when disclosed**; never infer a salary to satisfy the filter |
| `sensitive_field_policy` | String(20) | no | `SENSITIVE_FIELD_POLICIES`: `never_answer` \| `ask_every_time` \| `use_saved_answer` \| `prefer_decline` |
| `eeo_policy` | String(20) | no | always one of `never_answer` \| `prefer_decline`; **`use_saved_answer` is rejected** for EEO keys ([08 §1](08-application-fields.md)) |
| `credential_policy` | String(20) | no | `never_create` \| `create_on_demand` \| `reuse_only` (the shipped `application.auto_create_credentials`) |
| `schedule` | JSON | no | `{cadence_seconds, quiet_hours:{start,end,timezone}, days[], timezone}` |
| `notify` | JSON | no | `{kinds:[…], channels:["in_app"], digest:"immediate"\|"daily"\|"weekly"}` — a subset of `NOTIFICATION_KINDS` |
| `consents_required` | JSON | no | `["terms","data_processing","automation"]` — what this workflow needs |
| `consent_snapshot` | JSON | no | `{key: accepted_at}` frozen at the last evaluation that acted |
| `disclosure_version` | String(24) | no | the version of the disclosure text accepted; a text change invalidates the snapshot and re-asks |
| `expires_at` | DateTime | yes | a policy may be time-boxed ("auto-apply for two weeks") |
| `version` | Integer | no | bumped on every change |
| `updated_by` | String(20) | no | `ACTOR_TYPES` |
| `created_at` / `updated_at` | DateTime | no/yes | |
| `effective_from` | DateTime | no | |

Constraints: `UNIQUE(user_id, scope, scope_key, workflow)`; `INDEX(user_id,
workflow, enabled)`.

### 2.1 `automation_policy_revisions` (append-only)

`{id, user_id, policy_id, version, document (JSON of the whole row), changed_keys[],
actor_type, actor_id, reason, request_id, created_at}` — `UNIQUE(user_id,
policy_id, version)`.

Why: "who turned auto-submit on, and when?" must be answerable without reading
`audit_logs` prose, and an application row's `policy_id` must resolve to *the
policy as it was* (`applications.consent_snapshot` freezes the consents; the
revision freezes the limits).

---

## 3. Defaults (per plan)

| Workflow | Free | Pro | Pro+ |
|---|---|---|---|
| `discovery` | `suggest`, cadence off (`can_use_scheduled_workflows = false`) | `prepare`, 6 h | `prepare`, 2 h |
| `funding` | `off` | `suggest`, 24 h | `suggest`, 12 h |
| `application_prep` | `off` | `prepare`, 12 h | `prepare`, 6 h |
| `application_submit` | `off` | `off` (opt-in per user) | `off` (opt-in per user) |
| `outreach` | `off` | `prepare` (draft only) | `prepare` |
| `profile_refresh` | `off` | `suggest`, 30 d | `suggest`, 14 d |

`application_submit` is **never** on by default on any plan, and enabling it
requires: `AUTOFILL_ENABLED` ∧ ¬`AUTOFILL_DRY_RUN` ∧ `AUTOFILL_ALLOW_SUBMIT`
(server), `can_use_autofill` (plan), the `automation` consent (user), and
`allow_auto_submit` (the shipped per-user setting, which becomes
`policy.mode = auto_submit`). That chain is already enforced in
`POST /api/jobs/{id}/apply`; the contract names it so it is checked in the same
order everywhere.

---

## 4. Sensitive-field policy semantics

| Value | Meaning | Applies to |
|---|---|---|
| `never_answer` | leave blank; if the field is `required`, the application goes `blocked_input` | any `sensitive`/`restricted` key |
| `ask_every_time` | always create an input request, even when a saved answer exists | default for `restricted`, and for `currentCompensation`, `dateOfBirth`, `securityClearance` |
| `use_saved_answer` | reuse within the answer's `reuse_scope` ([08 §5](08-application-fields.md)) | `sensitive` only; **rejected for `restricted`** |
| `prefer_decline` | choose the portal's decline/"prefer not to say" option when it exists, otherwise `never_answer` | default for `EEO_FIELD_KEYS` |

The evaluation must record which policy applied to which field, in
`application_field_answers.resolution` (`policy_default`) — a silent decline and
a chosen decline look the same to a portal and must not look the same to us.

---

## 5. `evaluate_policy` — the decision contract

Every actor that is about to do something on the user's behalf calls this. It is
pure: same inputs → same decision, and the decision is *stored* on the work item.

```json
{
  "allowed": false,
  "mode": "prepare",
  "workflow": "application_submit",
  "policy_id": 12,
  "policy_version": 4,
  "decided_at": "2026-09-18T14:03:22Z",
  "reason": "consent_missing",
  "blockers": [
    { "kind": "consent", "key": "automation", "fix": "/account?tab=consents" }
  ],
  "limits": { "runs_today": 3, "max_runs_per_day": 10,
              "runs_this_month": 41, "max_runs_per_month": 50,
              "quota_remaining": 9 },
  "inputs": {
    "server": { "autofill_enabled": true, "autofill_dry_run": false, "autofill_allow_submit": true },
    "plan":   { "plan": "pro", "can_use_autofill": true, "can_use_scheduled_workflows": true },
    "consent":{ "terms": "2026-08-01T10:00:00Z", "data_processing": "2026-08-01T10:00:00Z", "automation": null },
    "policy": { "scope": "global", "mode": "auto_submit", "require_review_before_submit": true }
  },
  "downgrade": { "to": "prepare", "why": "consent_missing" }
}
```

`reason` vocabulary (never free text):

| `reason` | Meaning | Downgrade |
|---|---|---|
| `allowed` | act as `mode` says | — |
| `server_disabled` | a deployment flag forbids it | to `prepare` or `off` |
| `plan_locked` | the plan does not include the capability | to `suggest`/`off` + `402 upgrade_required` |
| `consent_missing` / `consent_withdrawn` / `disclosure_version_changed` | the gate is not satisfied | to `prepare`, and `blocked_consent` for an in-flight application |
| `policy_off` | the user turned this workflow off | none — nothing runs |
| `daily_limit` / `monthly_limit` / `quota_exhausted` | a ceiling is reached | none, `blocked_quota` |
| `quiet_hours` | the schedule says not now | defer (`scheduled_at` moved) |
| `company_blocked` / `portal_not_allowed` / `keyword_blocked` / `location_excluded` / `score_below_floor` / `compensation_below_floor` | the posting is out of policy | `blocked_policy` |
| `review_required` | `require_review_before_submit` | to `awaiting_approval` |
| `expired` | `expires_at` passed | none |

Rules:

1. **A downgrade is recorded, never silent.** `downgrade.to` + `why` go on the
   queue payload and into the application event, so "why did it only prepare?" has
   an answer.
2. **Evaluation happens twice**: at trigger time (to answer the click honestly)
   and at execution time (because a queued intent can outlive the consent or the
   quota it was created with — the shipped `execute_application` behaviour).
3. **The decision is stored** on the attempt (`policy_id`, `policy_version`,
   `consent_snapshot`) — see `applications` in
   [07 §2](07-application-state-machine.md).
4. **Quota is charged on the outcome, not the intent** — the rule the shipped
   funding scan and company-intel paths already follow (a paused or failed run
   costs nothing).

---

## 6. Consents and disclosures

Keys are the shipped `DISCLOSURES` map: `terms`, `automation`, `outreach`,
`data_processing`. The contract adds:

* `disclosure_version` — a semver per disclosure text, stored in
  `DISCLOSURES[key].version`. Changing the text bumps the version; a policy whose
  `disclosure_version` is older re-asks (reason
  `disclosure_version_changed`) instead of silently relying on a consent to
  different words.
* `accepted_at` / `revoked_at` timestamps stay on `User.consents` (shipped); the
  policy copies them into `consent_snapshot` at evaluation time.
* `automation` gates `application_submit` **and** browser navigation with
  credentials; `outreach` gates sending mail; `terms` + `data_processing` gate
  everything (they are onboarding gate 2).
* Withdrawal is immediate and honoured by in-flight work at execution time; it
  does not retroactively invalidate a submission that already happened (the
  snapshot is the record of what was true then).

---

## 7. Policy state machine

A policy row has no lifecycle states of its own — it is `enabled`/`disabled`
plus `expires_at` — but its **effect** does:

| # | From | To | Actor / trigger | Guard | Side effects | Event | Audit |
|---|---|---|---|---|---|---|---|
| P1 | *(none)* | `global` row created | `system_api` — account creation | — | plan defaults; `mode_chosen_at = null` | `automation.policy_changed` | `account.created` |
| P2 | any | new `version` | `user` — `PUT /api/automation/policy` | validation (EEO cannot be `use_saved_answer`; `auto_submit` requires the consent chain) | revision row; in-flight `queued` items re-evaluated at execution; scheduler cadence recomputed | `automation.policy_changed` | `automation.policy_updated` |
| P3 | `mode ≠ auto_submit` | `auto_submit` | `user` | consent `automation` accepted **and** server flags allow it **and** `can_use_autofill` | `mode_chosen_at` set; onboarding gate 7 completes | `automation.policy_changed` | `automation.auto_submit_enabled` *(sensitive)* |
| P4 | `auto_submit` | `prepare` | `user`, or `system_api` when the consent is withdrawn | — | queued submissions become `blocked_consent` at execution time | `automation.policy_changed` | `automation.auto_submit_disabled` |
| P5 | narrower row | deleted | `user` — `DELETE /api/automation/policy/{id}` | not the `global` row | resolution falls back to the next scope | `automation.policy_changed` | `automation.policy_deleted` |
| P6 | active | `expired` | `system_scheduler` — `expires_at` passed | — | workflow stops; notification `action_required` if the user set it up deliberately | `automation.policy_changed` | — |
| P7 | any | re-evaluated | `system_scheduler` — sweep | `enabled` ∧ cadence due ∧ not `quiet_hours` | `scheduled_runs` row (shipped) + queue item | `automation.run_scheduled` \| `automation.run_skipped` | — |
| P8 | any | quota stop | `system_worker` — ceiling reached | — | `scheduled_runs.state = skipped_quota`; notification `quota_exhausted` (deduped per window, shipped) | `automation.quota_exhausted` | — |

The shipped `scheduled_runs` table stays exactly as it is — it is the *history of
decisions*, and this policy is the *input* to them. `overview()` gains
`policy_id`/`policy_version` per workflow and nothing else changes.

---

## 8. API

* `GET /api/automation/policy?workflow=…` → the effective policy + the resolution
  (§5 decision shape, without `inputs` unless `?explain=1`).
* `PUT /api/automation/policy` → upsert a row; returns the whole effective policy.
* `GET /api/automation/policy/revisions` → paginated history.
* `GET /api/automation` (shipped) keeps its shape and gains
  `policy: {id, version, mode, effective_mode, blockers[]}` per workflow.

---

## 9. Invariants (tests)

1. Exactly one `global`/`*` row per `(user, workflow)`; created with the account.
2. Resolution is monotone: adding a narrower row can only reduce what is allowed,
   never increase it above the server/plan ceiling.
3. `mode = auto_submit` is impossible without all four gates (§3); attempting it
   returns `403 consent_required` or `402 upgrade_required` and writes no row.
4. `eeo_policy ∈ {never_answer, prefer_decline}` always; a PUT with
   `use_saved_answer` for an EEO key is `422 restricted_field`.
5. Every change writes exactly one revision row with the correct `changed_keys`.
6. An evaluation at execution time reflects a consent withdrawn after enqueue
   (the queued intent does not carry the old consent with it).
7. `applications.policy_id` + `policy_version` + `consent_snapshot` are set for
   every attempt that acted.
8. A decision with `allowed = false` never enqueues work; it records
   `automation.run_skipped` with the reason (the shipped `skipped_*` states).
9. Cross-tenant policy reads are `404`; a `scope_key` naming another tenant's
   persona/company is rejected at write time.

---

## 10. Assisted browser sessions (v2.2.21)

Auto-apply is not the only shape this policy governs. A user may also drive an
**assisted session**: a real browser, one isolated context per `(user, job)`,
that types only what the user has already confirmed and *stops the moment a human
is required*. The policy above is its ceiling; the session's own rules are
narrower still.

```
POST /api/application-sessions                      start (one live session per job)
POST /api/application-sessions/{id}/observe         fold a structural page observation in
GET  /api/application-sessions/actions              the actionable queue
POST /api/application-sessions/{id}/actions/{a}/handoff   mint a one-time handoff
POST /api/application-sessions/{id}/actions/{a}/complete  the human finished that step
POST /api/application-sessions/{id}/resume          checkpoint-validated resume
POST /api/application-sessions/{id}/submit          reserve the at-most-once ledger slot
```

**States** (`APPLICATION_SESSION_STATES`, `docs/contracts/07` §10): `created →
launching → preparing → active → awaiting_user | paused → resuming → completed`,
with `expired` / `failed` / `cancelled` terminal.

**Pauses** (`USER_ACTION_KINDS`). `awaiting_user` is always paired with a pending
`application_actions` row — a pause nobody can see is a stall. The action's
`kind` says who must act and where:

| Kind | Who acts | Where |
|---|---|---|
| `login` | the user | in the browser; a handoff token opens a single-use window |
| `mfa` | the user | in the browser; the code is never relayed |
| `captcha` | the user | in the browser; never solved, never outsourced |
| `unknown_field` / `ambiguous_field` | the user | answers in the app, then the pass continues |
| `sensitive_field` / `legal_question` | the user | answers in the app; EEO defaults to `decline_to_answer` |
| `session_expired` | the user | re-authenticate: fresh TTL, no reused state |
| `review_required` | the user | anything the classifier could not clear |

**What the assistant may do.** Type into fields whose verdict is `autofill`
(`FIELD_ACTIONS`) — a confirmed profile/plan value, or an answer the user gave
for that exact field. Nothing else. Unknown, ambiguous, restricted, legal and
EEO fields have no path into a fill instruction, and credentials, one-time codes
and CAPTCHA tokens have no path into the session row either: the checkpoint keeps
a value *fingerprint*, the working set is purged when the session ends, and the
API refuses a completion payload carrying a secret-looking field
(`422 restricted_field`).

**What it may never do.** Solve or relay a CAPTCHA; intercept, read or type an
MFA code; type or store a password; take over a session established elsewhere;
hide that it is automation (no stealth user-agent spoofing, no proxy rotation, no
access-control bypass — the shipped outbound policy and host binding still
apply). A bot check is a full stop, not an obstacle to route around.

**Resuming** validates the checkpoint first: the job still exists and still
belongs to the user, the posting URL and the posting's own application identity
are unchanged, the page the user is looking at is the session's employer and
portal host. Any mismatch is a refusal with the failing codes — the session does
not resume on a stale assumption. Fields already `filled` (by the assistant) or
`user_completed` (by the user in the browser) are never typed again, which is
what makes "resume" safe to press twice.

**Submitting** goes through the at-most-once ledger: a partial unique index on
`(user_id, job_id)` for live rows plus a per-attempt idempotency key. Auto-submit
requires all four gates of §3 — server `AUTOFILL_ALLOW_SUBMIT`/`DRY_RUN`, plan,
the user's `application.allow_auto_submit` consent, and no pending action or
failed checkpoint. In this release the default is *off*: a session fills and
stops, and the human clicks submit.

**Retention.** Persisted browser state (cookies only, never passwords or
`localStorage` secrets) is opt-in, encrypted with the per-user key, expires with
the session and is purged on expiry, cancel and account erasure. Screenshots are
off by default, capped by `BROWSER_SCREENSHOT_MAX_RETENTION_DAYS`, and are never
taken on a login, MFA or bot-check screen.
