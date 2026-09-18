# 03 — Resume document and extraction entities

**Deliverable 2.** Separates three things the shipped schema stores in one row:
the **file**, the **extraction attempt**, and the **fact** the attempt produced.

Why: today `resumes` holds `filepath`, `profile_snapshot`, `layout`, `status`,
`guardrail_report` and `text_snapshot` together, and the extraction *metadata*
(model, prompt version, tokens, per-field confidence, evidence spans) is
returned by `POST /api/resume/upload` and then thrown away — only
`Profile.extraction_source = "ai"` survives. That means:

* a re-parse of the same file cannot be compared with the previous parse;
* "why does my profile say X?" has no answer after the request ends;
* the upload is not resumable — it is one long HTTP request whose progress lives
  in an in-process dict (`_upload_progress` in `app/api/routers/resumes.py`),
  which a restart, a second replica or a page refresh loses;
* a tailored resume and the master resume it came from share a `status`
  vocabulary that means different things for each.

---

## 1. Entity map

```
resume_documents  (the file: bytes, hash, text snapshot, layout, role, state)
   ├── resume_extractions   (one row per attempt: extractor, model, prompt, tokens, verdict)
   │      └── extracted_fields   (one row per fact the attempt produced, with evidence)
   ├── candidate_profiles   (the profile version an extraction produced)  [02]
   └── applications         (which document was attached)                 [07]
```

`resume_documents` is the **renamed and extended** `resumes` table
([14 §3](14-migration-strategy.md) keeps the old name as a view/alias for one
minor version, so nothing breaks).

---

## 2. `resume_documents`

| Column | Type | Null | Meaning |
|---|---|---|---|
| `id` | Integer PK | no | |
| `user_id` | FK, index | no | tenant |
| `persona_id` | FK `personas.id` | yes | the track this document was tailored for |
| `role` | String(20) | no | `RESUME_ROLES`: `master` \| `tailored` \| `polished` \| `cover_letter` \| `attachment`. Legacy `type` values map: `generated`→`tailored`, `uploaded_polished`→`polished`. |
| `state` | String(20) | no | `RESUME_STATES` (§5) |
| `filename` | String(300) | no | sanitised upload name (`safe_upload_name`) |
| `display_name` | String(300) | no | the professional download name |
| `filepath` | String(600) | no | opaque storage path; never exposed |
| `content_type` | String(80) | no | `application/pdf` \| `…wordprocessingml.document` |
| `size_bytes` | Integer | no | |
| `sha256` | String(64) | no | content hash — dedupe a re-upload, prove what was sent to a portal |
| `page_count` | Integer | yes | |
| `text_snapshot` | Text | no | rendered/extracted text (shipped column) |
| `text_sha256` | String(64) | no | hash of `text_snapshot` |
| `layout` | JSON | no | the deterministic layout report (`extract_layout`) |
| `profile_snapshot` | JSON | no | **immutable** copy of the profile document at render time — a tailored resume must say what it claimed even after the profile changes |
| `tags` | JSON | no | string list, ≤ 20 × 60 chars |
| `parent_document_id` | FK `resume_documents.id` | yes | the master/polished parent (was `parent_resume_id`) |
| `job_id` | FK `jobs.id` | yes | the posting it was tailored for |
| `jd_hash` | String(64) | yes | `sha256` of the JD text used for the reuse-similarity decision |
| `application_id` | FK `applications.id` | yes | the submission it was attached to |
| `guardrail_report` | JSON | no | accuracy + ATS verdict (shipped column) |
| `fact_guard_report` | JSON | no | fabrication check result (`fact_guard_check`) — **separate** from the guardrail: one is "is this good?", the other is "is this true?" |
| `created_by` | String(20) | no | `ACTOR_TYPES` — `user` (upload/polish), `system_worker` (generation) |
| `pipeline_job_id` | FK `pipeline_jobs.id` | yes | the queued work that produced it (resumability + "why is this here?") |
| `created_at` / `updated_at` | DateTime | no/yes | |
| `approved_at` | DateTime | yes | actor: `user` |
| `archived_at` / `deleted_at` | DateTime | yes | `deleted_at` is a tombstone: the row survives the file's removal so an application can still say what it sent |

Constraints: `UNIQUE(user_id, sha256, role)` (a re-upload of identical bytes is
a no-op that returns the existing row), `INDEX(user_id, role, state)`,
`INDEX(user_id, job_id)`.

**Exactly one `master` in state `approved`** may be current per user; uploading a
new master moves the previous one to `archived` (it is never deleted — tailored
resumes point at it).

---

## 3. `resume_extractions` — one row per attempt

Append-only. An attempt is created *before* the model is called, so a crash
leaves a `failed`/`running` row instead of nothing — that is what makes the
upload resumable and what replaces the in-memory progress dict as the durable
record.

| Column | Type | Null | Meaning |
|---|---|---|---|
| `id` | Integer PK | no | |
| `user_id` | FK, index | no | tenant |
| `resume_document_id` | FK, index | no | the file |
| `attempt` | Integer | no | 1-based per document; `UNIQUE(user_id, resume_document_id, attempt)` |
| `kind` | String(24) | no | `profile_extraction` \| `layout_extraction` \| `text_extraction` \| `tailoring` \| `tagging` \| `fact_check` |
| `state` | String(16) | no | `EXTRACTION_STATES`: `pending` → `running` → `succeeded` \| `failed` \| `rejected` \| `superseded` |
| `pipeline_job_id` | FK `pipeline_jobs.id` | yes | the durable queue item; `dedupe_key = extract:{document_id}:{extractor_version}` |
| `extractor` | String(60) | no | e.g. `ai_profile_extractor`, `pdfminer_text`, `docx_text` |
| `extractor_version` | String(24) | no | semver — a bump is what makes an old extraction `superseded` |
| `model` | String(120) | yes | provider model id |
| `prompt_version` | String(60) | yes | e.g. `profile-2026-08-30` |
| `profile_id` | FK `candidate_profiles.id` | yes | the profile version produced (only for `profile_extraction`) |
| `input_sha256` | String(64) | no | `text_sha256` of the input |
| `output_sha256` | String(64) | yes | hash of the produced document |
| `field_count` | Integer | no | how many `extracted_fields` rows were written |
| `needs_review_count` | Integer | no | how many of them need a human |
| `overall_confidence` | Float | yes | |
| `guardrail_report` | JSON | no | |
| `fact_ledger` | JSON | no | the entity ledger the guardrail checked against the source text (`build_fact_ledger`) |
| `tokens` | JSON | no | `{prompt, completion, total}` — mirrors `ai_credit_ledger` |
| `latency_ms` | Integer | no | |
| `error_code` | String(40) | yes | from `ERROR_CODES` (`ai_unavailable`, `guardrail_failed`, `no_readable_text`, `payload_too_large`) |
| `error_message` | Text | yes | human sentence, ≤ 2000 chars |
| `started_at` / `finished_at` | DateTime | yes | |
| `created_at` | DateTime | no | |
| `trigger` | String(16) | no | `QUEUE_TRIGGERS` (`user` upload, `retry`, `auto` re-extraction after an extractor upgrade) |

`state = rejected` means *the model answered and we refused the answer*
(guardrail or fact guard). `failed` means we did not get an answer. The
distinction matters because `rejected` is not retryable with the same inputs and
must show the user the violations, while `failed` is retryable and, for a
transient AI outage, becomes a queue `pause` rather than a failure
([11 §5](11-background-jobs.md)).

---

## 4. `extracted_fields` — what one attempt actually said

Append-only, immutable. This is the raw model output per field, *before* review;
`profile_field_provenance` ([02 §3](02-candidate-profile.md)) is the current
authoritative view and points back at the row here that produced it.

| Column | Type | Null | Meaning |
|---|---|---|---|
| `id` | Integer PK | no | |
| `user_id` | FK, index | no | tenant |
| `extraction_id` | FK, index | no | the attempt |
| `path` | String(400) | no | JSON Pointer into the produced document |
| `value` | JSON | no | the extracted value (encrypted at rest when `sensitivity = restricted`) |
| `value_type` | String(16) | no | `string` \| `number` \| `boolean` \| `date` \| `list` \| `object` |
| `sensitivity` | String(12) | no | `SENSITIVITY_LEVELS` |
| `origin` | String(24) | no | `resume_extraction` \| `ai_inferred` \| `document_text` |
| `confidence` | Float | yes | |
| `band` | String(8) | no | derived |
| `evidence` | JSON | no | `[{kind, quote, locator{page,start,end}, retrieved_at}]` — the span in `text_snapshot` |
| `ambiguous` | Boolean | no | the extractor saw more than one candidate (two emails, overlapping dates) |
| `candidates` | JSON | no | `[{value, confidence, evidence}]` when `ambiguous` — the review UI offers these instead of asking the user to retype |
| `review_status` | String(16) | no | as written; a later review updates `profile_field_provenance`, **not** this row |
| `created_at` | DateTime | no | |

Constraints: `UNIQUE(user_id, extraction_id, path)`, `INDEX(extraction_id,
review_status)`.

**Evidence span rule**: `locator.start`/`end` are character offsets into
`resume_documents.text_snapshot`, and `quote` must equal that slice (±
whitespace). A verification test asserts it on a fixture: an evidence span that
does not point at the text it quotes is worse than no evidence, because it looks
like proof.

---

## 5. Resume document state machine

| From | To | Actor / trigger | Guard | Side effects | Event |
|---|---|---|---|---|---|
| — | `uploading` | `user` — bytes accepted, before the row commits | type/size/magic-byte checks pass | file written to `upload_dir`; nothing else exists yet | — (no durable row yet) |
| `uploading` | `stored` | `system_api` — row committed | `sha256` computed; no identical `(user_id, sha256, role)` row | extraction attempt created (`pending`), queued | `resume.uploaded` |
| `uploading` | *(discarded)* | `system_api` — validation/parse/AI failure | — | file removed, no row (the shipped `keep_file` guard) | — |
| `stored` | `extracting` | `system_worker` — extraction claimed | lease held | `resume_extractions.state = running` | `extraction.started` |
| `extracting` | `approved` | `system_worker` — extraction succeeded, `role = master`, nothing needs review | guardrail + fact guard passed | profile version created & `is_current`; onboarding `resume` gate completes | `extraction.succeeded` |
| `extracting` | `pending_approval` | `system_worker` — extraction succeeded but ≥1 field needs review, or `role = tailored` | — | onboarding `profile_review` gate becomes blocking; notification `profile_review_required` / `resume_ready` | `extraction.succeeded` |
| `extracting` | `rejected` | `system_worker` — guardrail or fact guard refused the output | `fact_guard_report.passed = false` or guardrail issues | **nothing** is written to the profile; violations stored on the extraction; user sees them | `extraction.rejected_by_guardrail` |
| `extracting` | `stored` | `system_watchdog` — transient AI outage | `paused_count < AI_PAUSE_MAX` | queue item paused, no attempt spent ([11 §5](11-background-jobs.md)) | `job.paused` |
| `extracting` | `stored` | `system_worker` — non-AI failure, retries remain | `attempts < max_attempts` | queue item re-queued with backoff | `job.failed` |
| `pending_approval` | `approved` | `user` — `POST /api/resumes/{id}/approve` | review gate satisfied for the fields this document feeds | `approved_at`; a tailored resume becomes reusable for JD-similar postings; the application it blocks is unblocked | `resume.approved` |
| `pending_approval` | `rejected` | `user` — `POST /api/resumes/{id}/reject` | — | a blocked application stays blocked with `blocked_resume_approval` → `failed` only if the user cancels it | `resume.rejected` |
| `approved`, `rejected` | `archived` | `user` or `system_worker` — superseded by a new master, or aged out | not referenced by a live application | download links stop resolving; the row remains for provenance | `resume.archived` |
| any | `deleted` | `user` — `DELETE /api/resumes/{id}` | references detached first (`erasure.break_references`), else `409 resume_referenced` | files removed after commit; `profile_snapshot` kept; tombstone for applications that used it | `resume.deleted` |

Terminal: `deleted`. `archived` is reversible by `user` (`POST
/api/resumes/{id}/restore`, proposed) — a document that was merely superseded is
not gone.

---

## 6. Upload / extraction flow (resumable)

The shipped `POST /api/resume/upload` is one long request. The contract splits it
so no step is lost:

```
POST /api/resume/upload            (multipart)
  → validate (type, magic bytes, size)          [sync, cheap, user-fixable → 415/413/422]
  → store file, INSERT resume_documents(stored) [sync, one transaction]
  → INSERT resume_extractions(pending)          [sync]
  → enqueue pipeline=extraction,
      dedupe_key=extract:{doc_id}:{extractor_version},
      payload.checkpoint={step: "extracting_text"}
  → 202 {resume_document_id, extraction_id, pipeline_job_id, idempotency_key}

worker: text → layout → AI profile → guardrails → fact ledger → provenance
        writes payload.checkpoint after each step (§7 of 11)
        on success: candidate_profiles(draft|active) + extracted_fields + resume_documents.state

GET /api/resume/progress            (shipped, in-memory) → kept as a *live* convenience view
GET /api/extractions/{id}           (proposed)           → the durable record, readable after a restart
```

Backwards compatibility: the shipped endpoint keeps returning
`{resume, profile, layout, ai_meta, search_context, persona, rescored_jobs}`
for one minor version while it *also* returns the receipt keys
(`resume_document_id`, `extraction_id`, `pipeline_job_id`), and `GET
/api/resume/progress` keeps working but is documented as best-effort: the
durable answer is the extraction row.

**Rule**: any step that can take longer than the HTTP timeout (AI extraction,
tailoring, tagging) must be queued. The only synchronous steps are the cheap,
user-fixable validations — the pattern `POST /api/resumes/generate` already
follows since v2.2.8.

---

## 7. Ownership and invariants

| Entity | Single writer | Readers |
|---|---|---|
| `resume_documents` | `app/services/resume_service.py` (shipped) + the upload route for the create transition | applications, downloads, diff/preview, export |
| `resume_extractions` | `app/services/resume_parser.py` via the extraction handler | onboarding, review UI, ops ("why is this profile wrong?") |
| `extracted_fields` | the same handler, one bulk insert per attempt | review UI, provenance rows |

Invariants (tests):

1. A `resume_documents` row exists **iff** its file exists or `deleted_at` is set.
2. `state = approved` requires a `succeeded` extraction for `role = master`, and
   a `passed` fact guard for `role = tailored`.
3. Every `extracted_fields` row's `evidence[].quote` matches the
   `locator` slice of the parent document's `text_snapshot`.
4. Re-uploading identical bytes returns the existing row
   (`UNIQUE(user_id, sha256, role)`) and creates **no** new extraction.
5. A `rejected` extraction never produces a profile version.
6. Deleting a document referenced by a non-terminal application is
   `409 resume_referenced` with the blockers named (the shipped behaviour).
7. Two tenants' documents never collide: every query above is `user_id`-scoped,
   and the signed download token binds `(resume_id, user_id, format)`.
