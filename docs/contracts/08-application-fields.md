# 08 — Ambiguous and sensitive application fields

**Acceptance criterion: "Ambiguous and sensitive application fields are
represented explicitly."** This is the contract that makes that true — an
application field is not "filled" or "empty", it carries a classification, a set
of candidates, a resolution, a sensitivity level and a reuse scope.

The shipped code already has the right instincts and no vocabulary for them:
`build_autofill_plan` records a `value_source` per field and leaves
"voluntary/demographic questions … blank unless the user provided an explicit
answer — never invented", and `_value_for` accepts an answer keyed either by the
canonical profile key or by the raw form field name. This document names those
behaviours and closes the gaps.

---

## 1. The field registry

Every application form field is mapped to a **canonical key** (the shipped
`KNOWN_FIELDS` / `PROFILE_PATHS` names) or stays `null` (unknown → the user is
asked, and the raw label is preserved). The registry fixes the sensitivity, the
default policy and how often the field turns out to be ambiguous.

| Canonical key | Portal labels (examples) | Type | Sensitivity | Profile path | Default policy | Ambiguity risk |
|---|---|---|---|---|---|---|
| `firstName` | First name, Given name | string | `public` | `/identity/first_name` | fill from profile; derive from `full_name` | low |
| `lastName` | Last name, Surname, Family name | string | `public` | `/identity/last_name` | as above | **medium** — particle surnames ("van der Berg"), two-word last names |
| `fullName` | Full name, Your name | string | `public` | `/identity/full_name` | fill | low |
| `preferredName` | Preferred name, What should we call you? | string | `public` | `/identity/preferred_name` | fill if confirmed, else leave | low |
| `pronouns` | Pronouns | select | `sensitive` | `/identity/pronouns` | **ask** — never inferred from a name | medium |
| `email` | Email, E-mail address | email | `sensitive` | `/contact/email` | fill from a `confirmed` value only | **medium** — a profile may hold a personal and a work address |
| `phone` | Phone, Mobile, Contact number | tel | `sensitive` | `/contact/phone` | fill from `confirmed`; ask if two numbers exist | **high** — personal vs work, country-code format |
| `location` | Location, City, Where are you based? | string | `sensitive` | `/contact/location/city` | fill | **high** — current city vs. target city vs. willingness to relocate |
| `address` | Address, Street | string | `sensitive` | `/contact/address` | **ask** — rarely on a resume | medium |
| `linkedin` | LinkedIn profile | url | `public` | `/contact/kind=linkedin` | fill | low |
| `github` | GitHub, Portfolio, Website | url | `public` | `/contact/kind=github` | fill | **medium** — three different links can match one label |
| `resume` | Resume, CV | file | `internal` | — | attach the chosen `resume_document_id` | low |
| `coverLetter` | Cover letter, Letter of interest, Additional information | file/text | `internal` | — | attach only when the user asked for one | **medium** — "Additional information" is not a cover letter |
| `salaryExpectation` | Expected salary, Compensation expectations | number/string | `sensitive` | `/compensation/target` | fill from a `confirmed` value; **ask** when the portal wants a single number and the profile holds a range | **high** — range vs point, gross vs net, period, currency |
| `currentCompensation` | Current salary | number | `sensitive` | `/compensation/current` | **ask every time**; never inferred; may be left blank where lawful | high |
| `noticePeriod` | Notice period, Availability, Earliest start | string/number | `sensitive` | `/availability/notice_period_days` | fill from `confirmed`; ask when the portal wants a date and we hold days | **high** — days vs date vs "immediately" |
| `workAuthorization` | Authorized to work in …, Right to work, Visa | select | `sensitive` | `/eligibility/work_authorization` | **ask** unless a confirmed answer exists *for this country* | **very high** — option lists differ per portal ("Yes", "Yes, with sponsorship", "No") |
| `sponsorshipRequired` | Will you require sponsorship? | select/boolean | `sensitive` | `/eligibility/sponsorship_required` | **ask**; the answer must be consistent with `workAuthorization` | very high |
| `relocationWilling` | Willing to relocate? | select/boolean | `sensitive` | `/eligibility/relocation_willing` | ask | high |
| `employmentType` | Desired employment type | select | `internal` | `/availability/employment_types` | fill | medium |
| `startDate` | Available from, Start date | date | `sensitive` | `/availability/start_date` | fill from `confirmed`; ask when only a notice period is known | high |
| `securityClearance` | Do you hold a security clearance? | select | `restricted` | `/eligibility/security_clearance` | **ask every time**, never stored in plain text | high |
| `gender` | Gender (voluntary self-identification) | select | `restricted` | — | `policy_default = decline_to_answer` | n/a |
| `race` / `ethnicity` | Race/ethnicity (voluntary) | select | `restricted` | — | `policy_default = decline_to_answer` | n/a |
| `veteranStatus` | Veteran status (voluntary) | select | `restricted` | — | `policy_default = decline_to_answer` | n/a |
| `disabilityStatus` | Disability status (voluntary) | select | `restricted` | — | `policy_default = decline_to_answer` | n/a |
| `dateOfBirth` | Date of birth | date | `restricted` | `/identity/date_of_birth` | **ask every time**; never inferred from a graduation year | medium |
| `governmentId` / `nationalId` / `passportNumber` / `visaNumber` | National ID, Passport number | string | `restricted` | — | **never auto-filled**; typed by the user into the form themselves | n/a |
| `referenceContact` | References | string | `restricted` | — | ask; never harvest a contact from the resume | medium |
| `portalPassword` | Password (login step) | password | `restricted` | vault only | from `vault_entries`, typed only on the matching domain | n/a |
| `otpCode` | One-time code | password/text | `restricted` | never stored | **always** ask (`blocked_code = missing_required_field`, `Email.status = needs_otp` is the shipped analogue) | n/a |
| *(unmapped)* | "Which of our products interests you most?" | free text | `internal` | — | ask; store the raw label as the field key | n/a |

Rules that fall out of the table:

1. A `restricted` field is **never** filled from a profile value, never inferred,
   never reused across applications, and never appears in a log, an audit detail
   or an event payload.
2. An EEO field's default is `decline_to_answer`, applied as
   `resolution = policy_default` — which is *recorded*, not silent, so the user
   can see we declined on their behalf and change it.
3. A field whose canonical key is `null` keeps its raw `name` and `label`
   verbatim and is always asked. Answers to unmapped fields are stored with
   `reuse_scope = this_portal` at best (the same label may mean something else
   elsewhere).
4. Answering `workAuthorization` and `sponsorshipRequired` inconsistently
   (e.g. "authorized" + "needs sponsorship" for a country where the user holds no
   permit) is a validation error at answer time, not a submission-time surprise.

---

## 2. `application_field_answers`

One row per (application, form field). This replaces both
`jobs.extra.autofill_plan.fields[*].value` and
`user_input_requests.fields[*].value` as the authoritative store; those two stay
as projections for one minor version.

| Column | Type | Null | Meaning |
|---|---|---|---|
| `id` | PK | no | |
| `user_id` | FK, index | no | tenant |
| `application_id` | FK, index | no | |
| `attempt_id` | FK `application_attempts.id` | yes | the attempt that produced the mapping |
| `field_key` | String(60) | no | canonical key, or `raw:{name}` when unmapped |
| `field_name` | String(200) | no | the portal's own `name`/`id` attribute |
| `field_label` | String(300) | no | the portal's own label — preserved verbatim, it is the evidence |
| `field_type` | String(24) | no | `text` \| `email` \| `tel` \| `url` \| `date` \| `number` \| `select` \| `radio` \| `checkbox` \| `textarea` \| `file` \| `password` |
| `required` | Boolean | no | from the form |
| `options` | JSON | no | the portal's option list for `select`/`radio` (≤ 24 × 200 chars) |
| `sensitivity` | String(12) | no | `SENSITIVITY_LEVELS` — from the registry, or `internal` when unmapped |
| `ambiguity` | String(24) | no | `AMBIGUITY_KINDS` |
| `ambiguity_detail` | JSON | no | why: `{reason, conflicting_paths[], option_mismatch{}}` |
| `candidates` | JSON | no | `[{value, label, origin, confidence, band, evidence[], provenance_path}]`, best first, ≤ 6 |
| `value` | Text | yes | the resolved value; **encrypted at rest** when `sensitivity = restricted` |
| `value_hash` | String(64) | yes | for change detection without decrypting |
| `value_masked` | String(60) | yes | display-safe form (`+49 ••• •••• 123`) for `sensitive`/`restricted` |
| `resolution` | String(20) | no | `FIELD_RESOLUTIONS` |
| `resolution_actor` | String(20) | yes | `ACTOR_TYPES` — who resolved it |
| `resolved_at` | DateTime | yes | |
| `provenance` | JSON | no | the `Provenance` object of the chosen value ([01 §6](01-conventions.md)) |
| `provenance_id` | FK `profile_field_provenance.id` | yes | the profile field it came from — lets a profile correction invalidate the answer |
| `review_status` | String(16) | no | copied from the provenance row; `needs_review` blocks submission |
| `reuse_scope` | String(20) | no | `ANSWER_REUSE_SCOPES` |
| `reuse_key` | String(200) | yes | what the scope binds to (`greenhouse`, `wirelane`, `*`) — `UNIQUE(user_id, field_key, reuse_key)` for reusable answers |
| `reuse_count` | Integer | no | how many applications have used it |
| `asked_at` / `answered_at` | DateTime | yes | |
| `created_at` / `updated_at` | DateTime | no/yes | |

Constraints: `UNIQUE(user_id, application_id, field_name)`; `INDEX(user_id,
application_id, resolution)`; `INDEX(user_id, review_status)`;
`INDEX(user_id, field_key, reuse_key)`.

### 2.1 The frozen legacy `value_source` vocabulary

`build_autofill_plan` already writes a `value_source` per mapped field. Those six
strings (`AUTOFILL_VALUE_SOURCES`) are **frozen** — they are in stored plan JSON,
in `job_events` payloads and in the SPA's `aiWork` cache, so they are read, never
rewritten. New rows carry a `resolution` as well; the mapping is fixed:

| Shipped `value_source` | `resolution` | `provenance.source` | Notes |
|---|---|---|---|
| `profile` | `profile` | the field's own provenance source | only when `review_status ≠ needs_review` |
| `resume_file` | `resume_file` | `document_text` | the value is the attachment itself, not a field |
| `generated_cover_letter` | `resume_file` | `ai_inferred` | a generated document; fact-guarded before use |
| `vault` | `vault_credential` | `user_provided` | encrypted at rest; never logged, never in a plan preview |
| `user_answer` | `user_typed` or `user_choice` | `user_provided` | `user_choice` when the answer came from a candidate list |
| `unknown` | `unresolved` | — | the field is open; `required = true` blocks the application |

A reader that meets a `value_source` it does not know renders the raw string and
treats the field as unresolved — never as filled ([01 §9](01-conventions.md)).

---

## 3. Ambiguity is a verdict, not a vibe

`ambiguity` is set by the mapper and must be one of:

| Value | Set when | What the user sees | Blocking? |
|---|---|---|---|
| `resolved` | exactly one candidate, `band ≥ medium`, sensitivity allows it | nothing — it is filled | no |
| `ambiguous_mapping` | the portal label matched more than one canonical key ("Location" → current city *or* target city) | "Which did they mean?" with both readings | yes if `required` |
| `ambiguous_value` | one key, several candidate values (two phones, personal + work email) | a picker over the candidates, each with its provenance and evidence | yes if `required` |
| `ambiguous_option` | our value does not appear in `options` (work authorization wording, "Prefer not to say" vs "Decline") | the portal's own option list, with the closest match pre-selected and marked as a guess | yes — always, because picking wrong here is a legal statement |
| `missing` | no value anywhere | a blank input with the label and the evidence that we looked | yes if `required` |
| `out_of_scope` | the registry/policy forbids answering | "We do not answer this automatically" + an explicit opt-in to type it | no — the field is left blank unless the user opts in |

A non-required ambiguous field does not block: it is left blank and reported in
`ambiguous_count`, because a form that asks "how did you hear about us?" must not
stop an application.

**`ambiguous_option` is never auto-resolved.** Similarity between our value and a
portal option is offered as a pre-selection with `band = low` and
`review.status = needs_review`; the click is the user's.

---

## 4. Answer lifecycle

| # | From | To | Actor / trigger | Guard | Event |
|---|---|---|---|---|---|
| F1 | — | `resolution = profile` | `system_worker` — the mapper found one confirmed value | `review_status ≠ needs_review`, sensitivity ≤ `sensitive` | `application.fields_mapped` |
| F2 | — | `resolution = resume_file` | `system_worker` — a `file` field bound to the chosen document | document `approved` | `application.fields_mapped` |
| F3 | — | `resolution = vault_credential` | `system_worker` — a password field bound to a vault entry | entry exists for `portal_domain`; **the value never enters this table** — only the `credential_id` | `application.credential_reused` |
| F4 | — | `resolution = policy_default` | `system_api` — the policy answered (EEO decline, `never_answer`) | the field is in the registry with a default | `application.fields_mapped` |
| F5 | — | `resolution = unresolved` | `system_worker` — ambiguity or missing | — | `application.field_ambiguous`, `application.input_requested` |
| F6 | `unresolved` | `user_choice` | `user` — picked a candidate | the candidate exists in `candidates` | `application.input_provided` |
| F7 | `unresolved` | `user_typed` | `user` — typed a value | validation for the field type; `restricted` values encrypted | `application.input_provided` |
| F8 | `unresolved`, `policy_default` | `declined` | `user` — "prefer not to say" | the field is optional **or** the portal accepts a decline option | `application.input_provided` |
| F9 | any | `profile` (re-mapped) | `user` — corrected the underlying profile field | provenance row updated | `profile.field_corrected` + `application.field_ambiguous` if the answer changes |
| F10 | any resolved | `unresolved` | `system_worker` — the profile field the answer came from was corrected/rejected, or the answer's `review_status` became `needs_review` | — | `application.field_ambiguous`, `application.blocked` if it was submitted-ready |
| F11 | any | *(reused)* | `system_worker` — a new application in the answer's `reuse_key` scope | `reuse_scope ≠ never/this_application`, `review_status ∈ {confirmed, corrected, user_provided}`, sensitivity ≤ `sensitive` | `application.fields_mapped` with `provenance.source = user_confirmed` |

F10 is the important one: an answer is **derived state**, and derived state must
be invalidated when its input changes. A user who corrects their phone number in
the profile must not have three half-submitted applications still carrying the old
one.

---

## 5. Reuse scopes

| Scope | `reuse_key` | Reused for | Example |
|---|---|---|---|
| `never` | — | nothing | a one-off answer to a weird question |
| `this_application` | — | this row only | EEO answers (the default), `otpCode` |
| `this_portal` | portal id | any application on the same ATS | "How did you hear about us?" → `greenhouse` |
| `this_company` | `company_name_normalized` | any application to that company | their internal candidate id |
| `global` | `*` | every application | notice period, salary expectation, work authorization **for a given country** (`reuse_key = "workAuthorization:DE"`) |

Rules:

* Reuse requires `review_status ∈ {confirmed, corrected, user_provided}` — an
  `auto_accepted` value is used once and then must be confirmed to travel.
* Reuse of a `restricted` value is always refused, whatever the scope says.
* Reuse is recorded (`reuse_count`, and `provenance.source = user_confirmed` with
  `evidence[0].kind = "user_statement"` pointing at the original answer), so an
  application can show *"Notice period: 60 days — you told us on 12 Aug, reused
  4×"*.
* The user can revoke reuse (`PUT /api/answers/{id} {reuse_scope: "never"}`),
  which does not delete the answer for the application it was given for.

---

## 6. What the UI must render

For every field in the review/answer surface ([13 §6](13-api-response-shapes.md)):

1. the portal's **own label** and its field name (not our canonical key — the user
   has to recognise the form);
2. the value we intend to send, or `masked` for `sensitive`/`restricted`;
3. `ambiguity` as a reason sentence, and `candidates` as choices with their
   `provenance.source`, `band` and `evidence[0].quote`;
4. `sensitivity` as a badge for `sensitive`/`restricted`, with the never-inferred
   rule stated for `restricted`;
5. `reuse_scope` as a control, defaulting per §1;
6. `required` — because that is what decides whether it blocks.

And it must never render a `restricted` value it was not given, must never offer
"auto-fill all" for `restricted` fields, and must show a `needs_review` field as
unconfirmed even when it has a value.

---

## 7. Invariants (tests)

1. Every `application_field_answers` row has `ambiguity`, `resolution`,
   `sensitivity` and `reuse_scope` set — none of them nullable, none of them
   defaulted silently.
2. `resolution = unresolved` ⇔ the application's `phase = blocked` with
   `blocked_code ∈ {missing_required_field, ambiguous_field,
   restricted_field_needs_choice}` (when the field is required).
3. `sensitivity = restricted` ⇒ `resolution ∈ {user_typed, declined,
   policy_default}` and `reuse_scope ∈ {this_application, never}` and the value is
   encrypted and absent from every event/audit/log payload.
4. `ambiguity = ambiguous_option` ⇒ `resolution ≠ profile` (it must have been
   chosen or typed).
5. A candidate's `evidence[].quote` resolves against the profile document or the
   source file.
6. Reuse never crosses tenants, never crosses `reuse_key`, and never applies to a
   `needs_review` value.
7. Correcting a profile field invalidates exactly the answers whose
   `provenance_id` points at it (F10) — asserted end to end.
8. `fields_mapped + missing_required_count + ambiguous_count ≤ fields_total`, and
   the three counters agree with the answer rows (they are denormalisations, and
   a denormalisation that disagrees is a bug).
