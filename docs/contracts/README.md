# JobHunter shared domain contracts

**Contract version 1.0.0** · status: **Accepted for implementation** · owner: platform/domain architect
Machine-readable half: [`backend/app/contracts/vocabulary.py`](../../backend/app/contracts/vocabulary.py) ↔ [`frontend/src/lib/contracts.ts`](../../frontend/src/lib/contracts.ts)

This directory is the specification other engineers implement against. It defines
the canonical data model, the state machines, the API response shapes and the
event vocabulary for **discovery, matching, application, onboarding and
reporting**. It deliberately does *not* implement them: nothing here changes a
running endpoint, and every table marked *Proposed* does not exist in the
database yet.

Read it in this order:

| # | Document | Defines | Deliverable |
|---|---|---|---|
| — | [01-conventions.md](01-conventions.md) | tenancy, ids, timestamps, errors, pagination, idempotency, provenance/confidence/evidence, sensitivity, resumability, "server is the source of truth" | cross-cutting |
| — | [02-candidate-profile.md](02-candidate-profile.md) | candidate profile entities + field-level provenance + review lifecycle | 1 |
| — | [03-resume-and-extraction.md](03-resume-and-extraction.md) | resume document, extraction attempt, extracted field | 2 |
| — | [04-onboarding-state-machine.md](04-onboarding-state-machine.md) | onboarding session, gates, transitions, actors | 3 |
| — | [05-normalized-job.md](05-normalized-job.md) | normalized job schema, identity/dedupe, discovery run | 4 |
| — | [06-match-result.md](06-match-result.md) | match result, rubric, evidence, staleness | 5 |
| — | [07-application-state-machine.md](07-application-state-machine.md) | application entity + full state machine + submission idempotency | 6 |
| — | [08-application-fields.md](08-application-fields.md) | ambiguous & sensitive application fields, answers, reuse scopes | 6 (fields) |
| — | [09-application-events.md](09-application-events.md) | application event schema + event type vocabulary | 7 |
| — | [10-automation-policy.md](10-automation-policy.md) | user automation policy, effective-policy resolution, consent binding | 8 |
| — | [11-background-jobs.md](11-background-jobs.md) | background-job status contract, progress, checkpoints, cancellation | 9 |
| — | [12-events-and-notifications.md](12-events-and-notifications.md) | domain event envelope, notification contract, kinds, channels | 10 |
| — | [13-api-response-shapes.md](13-api-response-shapes.md) | onboarding status, profile review, discovery runs, job matches, application progress, user-action-required | 11 |
| — | [14-migration-strategy.md](14-migration-strategy.md) | expand → migrate → contract, backfills, dual-write, rollback, erasure | migration |
| — | [15-entity-ownership.md](15-entity-ownership.md) | every entity: single writer, readers, lifecycle, retention, deletion | ownership |

> **Reporting methodology** lives in [`docs/REPORTING.md`](../REPORTING.md) —
> windows and timezones, the metric definitions, minimum sample sizes, the
> calibration refusal and the export privacy gate. §11.7 of contract 07 is its
> normative summary.

---

## 1. The eleven rules every implementer follows

1. **Tenant isolation is a column, not a filter.** Every tenant-owned table has
   `user_id INTEGER NOT NULL` + an index, every query is scoped by the
   authenticated identity (`app/api/deps.py`), and no endpoint accepts a
   `user_id`/`tenant_id` from the client. See §3 of
   [01-conventions.md](01-conventions.md).
2. **Every row is timestamped.** `created_at` always; `updated_at` on mutable
   rows; explicit business timestamps (`submitted_at`, `reviewed_at`,
   `occurred_at`) rather than "whatever `updated_at` says".
3. **Every state change leaves an append-only trace** — a domain event *and*,
   when it is security-relevant, an `audit_logs` row with a named action from
   the audit vocabulary.
4. **Every extracted value carries provenance, confidence and evidence.**
   One shared object shape (`Provenance`, §6 of
   [01-conventions.md](01-conventions.md)) — never three ad-hoc ones.
5. **Sensitive and restricted values are classified at write time** and the
   classification decides storage, display, logging and whether automation may
   use them (§7 of [01-conventions.md](01-conventions.md)).
6. **Every transition has an initiating actor or system event.** A transition
   row without an actor column is not a contract, it is a guess.
7. **Long-running work is resumable.** A checkpoint after every unit of work, a
   dedupe/idempotency key on the trigger, and a re-run that cannot duplicate a
   side effect ([11-background-jobs.md](11-background-jobs.md)).
8. **The server owns all state.** The browser may cache, optimistically render
   and remember an id in `sessionStorage`; it may never be the only place a
   status, a plan or an answer exists.
9. **Names come from the vocabulary module.** If a string is stored in a status,
   kind, state, source or code column, it is declared in
   `app/contracts/vocabulary.py` and mirrored in `lib/contracts.ts`.
10. **New entities are additive.** Nothing in this contract set removes a
    column, renames a stored value or breaks a shipped endpoint; where a legacy
    shape must survive, the contract defines the *projection* onto it
    (`jobs.status`, §5 of [07-application-state-machine.md](07-application-state-machine.md)).
11. **Honest degradation.** A missing AI verdict, an unverified contact, a
    keyword-overlap score, a dry-run application and an unconfirmed portal
    submission are each labelled with their own value — never presented as the
    confident thing.

---

## 2. Status of each piece

Nothing here is implemented yet unless the table says so. "Shipped" means the
column/endpoint exists today and the contract only *pins* it.

| Piece | Tables | Endpoints | Status |
|---|---|---|---|
| Candidate profile | `profiles` (shipped), `candidate_profiles`, `profile_field_provenance` | `GET /api/profile/current` (shipped) | Proposed |
| Resume & extraction | `resumes` (shipped), `resume_documents`, `resume_extractions`, `extracted_fields` | upload/generate/approve (shipped) | Proposed |
| Onboarding | `onboarding_sessions`, `onboarding_events` | `GET /api/onboarding/status`, `POST /api/onboarding/*` | Proposed |
| Normalized job | `jobs` (shipped) + columns, `discovery_runs` | `GET /api/jobs*` (shipped), `GET /api/discovery/runs` | Partially shipped |
| Match result | `match_results` | `GET /api/jobs/{id}/matches` | Proposed (`score_*` columns shipped on `jobs`) |
| Application | `applications`, `application_field_answers`, `application_attempts` | `POST /api/jobs/{id}/apply` (shipped) | Proposed |
| Application events | `job_events` (shipped), `application_events` | `GET /api/jobs/{id}/events` (shipped) | Proposed |
| Application tracking | `application_tracking`, `application_tracking_events` | `GET/POST /api/application-tracking*` | **Shipped** ([07 §11](07-application-state-machine.md), [13 §8](13-api-response-shapes.md), [docs/APPLICATION_TRACKING.md](../APPLICATION_TRACKING.md)) |
| Automation policy | `settings` rows (shipped), `automation_policies` | `GET/PUT /api/settings` (shipped) | Proposed |
| Background jobs | `pipeline_jobs` (shipped) + `checkpoint`, `progress`, `idempotency_key` | `GET /api/queues/ai` (shipped) | Partially shipped |
| Events/notifications | `notifications` (shipped), `domain_events` | `GET /api/notifications` (shipped) | Partially shipped |
| API shapes §13 | — | six new/renamed reads | Proposed |

---

## 3. Shared contracts frontend and backend must both follow

This is the list the acceptance criteria asks for — the things that break when
one side changes them alone. Each is defined once, in the linked section, and
enforced by `backend/tests/test_contracts_vocabulary.py`.

| # | Shared contract | Where it lives | Who must follow it |
|---|---|---|---|
| 1 | **State/kind/code vocabulary** (every stored string) | `app/contracts/vocabulary.py` ↔ `lib/contracts.ts` | both |
| 2 | **Timestamp format** — ISO-8601 UTC with explicit offset (`2026-09-18T14:03:22Z`) | [01 §2](01-conventions.md) | both |
| 3 | **JSON field naming** — `snake_case` keys, `camelCase` only inside `profile.data`/form field keys (legacy, frozen) | [01 §1](01-conventions.md) | both |
| 4 | **Error envelope** — `{detail, code, message, request_id, …}` + the typed `ERROR_CODES` | [01 §5](01-conventions.md) | both |
| 5 | **Async trigger receipt** — `{queued, duplicate, pipeline_job_id, queue_status, idempotency_key}` | [11 §3](11-background-jobs.md) | both |
| 6 | **Queue item serialization** — `serialize_item()` / `live_view()` shape | [11 §2](11-background-jobs.md) | both |
| 7 | **`Provenance` object** — `{source, confidence, band, evidence[], extractor, review}` | [01 §6](01-conventions.md) | both |
| 8 | **Sensitivity + ambiguity classification** and the never-invent rule | [01 §7](01-conventions.md), [08](08-application-fields.md) | both |
| 9 | **`Idempotency-Key` header** semantics (send, store, replay) | [01 §8](01-conventions.md) | both |
| 10 | **Pagination envelope** — `{items, page, page_size, total, has_more}` | [01 §4](01-conventions.md) | both |
| 11 | **Polling cadence + visibility discipline** (4 s live queue, 20–30 s pages, pause when hidden) | [11 §7](11-background-jobs.md) | frontend |
| 12 | **Consent/disclosure keys** — `terms`, `automation`, `outreach`, `data_processing` | [10 §6](10-automation-policy.md) | both |
| 13 | **Audit action names** — `<aggregate>.<verb_past|verb>` | [12 §5](12-events-and-notifications.md) | backend |
| 14 | **Notification kinds + `link` routes** (SPA routes, not URLs) | [12 §3](12-events-and-notifications.md) | both |
| 15 | **Entitlement failure codes** — `402 upgrade_required`, `429 quota_exhausted`, `409 storage_cap_reached` | [01 §5](01-conventions.md) | both |
| 16 | **Unknown-value tolerance** — an unrecognised state/kind renders its raw string, never a blank or a crash | [01 §9](01-conventions.md) | frontend |
| 17 | **No secrets, no restricted values in payloads/logs** — masked or omitted | [01 §7](01-conventions.md) | both |
| 18 | **Route/URL stability** — new reads are additive; a renamed path keeps the old one for one minor version | [14 §6](14-migration-strategy.md) | both |

---

## 4. How to implement against this

* **Backend**: create the tables in one Alembic revision per phase
  ([14-migration-strategy.md](14-migration-strategy.md)), import names from
  `app.contracts`, write the single-writer service named in
  [15-entity-ownership.md](15-entity-ownership.md), emit the events listed in
  [09](09-application-events.md)/[12](12-events-and-notifications.md), and
  return exactly the shapes in [13](13-api-response-shapes.md).
* **Frontend**: import from `lib/contracts.ts`, render labels from its maps,
  derive state from the queue row (`lib/aiWork.ts` pattern) rather than from
  component state, and treat every `blocked_*`/`action_required` value as a
  link to `GET /api/actions/required`.
* **Both**: if a name you need is not in the vocabulary, add it to
  *both* modules and to the doc in the same commit. The drift test fails
  otherwise — that is the point.

## 5. What is explicitly out of scope

No new AI prompt, no new job source, no browser automation change, no billing
change, no change to the encryption scheme, no implementation of the endpoints
in [13](13-api-response-shapes.md). Where this contract set touches something
that already works, it pins the existing behaviour and says so.
