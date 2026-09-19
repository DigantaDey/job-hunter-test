# Application tracking — lifecycle and outcome feedback

The tracking layer is the product's **memory of what happened to an
application**: when it went out, who answered, whether there was an interview,
how it ended, and how we know. It is deliberately small — a state, a timeline and
a report — and deliberately not a CRM: there are no contacts, no companies, no
pipelines, no email sync.

The full normative contract is
[`docs/contracts/07` §11](contracts/07-application-state-machine.md). This page
is the plain-language version.

## Why the board was not enough

`jobs.status` is a queue column. It says what the *product* is doing about a job
right now — discovered, queued, preparing, applied, needs input — and it moves
forward as automations run. Three things fall out of that shape:

1. **It overwrites.** An application that got a reply, then an interview, then a
   rejection was, in the board's memory, only ever "applied" and then "rejected".
2. **It has no actor.** Nothing recorded whether the product saw a submission
   land or the person came back and typed "I got an interview".
3. **It forced analytics to guess.** The performance page computed interviews as
   "applied *and* score ≥ 75" and labelled the number "(est)" — a fabricated
   metric that flattered high-scoring jobs and ignored every real conversation.

Tracking keeps the board exactly as it is (it is still the projection that drives
the queue) and adds a second, honest record next to it.

## Two tables

```
application_tracking            one row per (user, job)
├── state, phase, state_origin   the current position, and who claims it
├── applied_at … outcome_at      first-occurrence timestamps, written once
├── match_* / artifact_*         snapshots frozen when the record opened
├── follow_up_at / note          the next thing you owe yourself
└── attribution                  source, channel, role family

application_tracking_events     append-only, gapless sequence per record
└── every fact: transition, note, follow-up, correction, snapshot refresh
```

Nothing is updated in place. The current state is a projection of the last event;
the timeline is the record of truth. Delete the row and you delete the history,
so the row is never deleted — a wrong belief gets a *correction* event instead.

```
not_applied → applied → recruiter_response → interview_scheduled
                                            → interview_completed
                                            → offer_received → withdrawn
                                            → rejected_by_employer (terminal)
                                            → withdrawn              (terminal)
```

`recruiter_response`, `interview_scheduled` and `interview_completed` accept a
self-loop: a second reply, a reschedule and another round are each a new fact,
and each one appends.

## Who is claiming this

Every event carries an origin, and the origin decides what the event is allowed
to mean.

| Origin | What it is | Trust |
|---|---|---|
| `system_observed` | the queue ran and saw it — a submission landed, the board moved | trusted |
| `user_reported` | the person said so, in the product | trusted |
| `verified_integration` | a separate, working integration — a calendar connection, a portal API | trusted |
| `email_inferred` | a heuristic reading of an inbox | **not trusted for outcomes** |

`interview_scheduled`, `interview_completed`, `offer_received` and
`rejected_by_employer` are trusted-only states. Asking for one of them with
`email_inferred` origin is refused with `422 origin_not_trusted` — the refusal is
the feature, and it is enforced by the vocabulary rather than by the absence of a
caller. **This release contains no email-parsing path at all.** What an inbox
heuristic may do, if one is ever wired up, is *propose*: a provisional
suggestion row that leaves the state alone and asks the person to confirm or
dismiss. Confirming records the fact as `user_reported`, because now a person
said so.

An interview is therefore counted when somebody recorded one — never because a
subject line looked promising.

## Status changes, corrections, duplicates

- **"I got an interview"** is one button on the job drawer and on `/tracking`. It
  asks for the slot, the format and an optional follow-up date, and writes
  `application.interview_reported` with `user_reported` origin.
- **Illegal moves are refused, not fudged.** `409 invalid_state_transition`
  returns the allowed list, and the UI renders its buttons from that same list —
  a button that cannot work is disabled with a reason rather than broken.
- **Corrections are events.** Saying "actually that was a phone screen, and it
  already happened" writes `application.state_corrected` with the old event id
  and a reason (three characters minimum). The original row stays, so the
  timeline shows what was believed and when it changed. A correction cannot
  reopen a terminal state — it records the truth about what already happened.
- **Replays are idempotent by fact.** Each event has a nullable dedupe key naming
  the fact (`trk:{id}:{event_type}:{state}:{minute}`), and writes accept an
  idempotency key. A retry, a double click or a restarted worker returns
  `200 duplicate: true` with an unchanged timeline — not a `409`, and never a
  second row.

## Follow-ups

A follow-up is a date you owe yourself: after applying, after an interview, after
a rejection, or just because. The scheduler sweep turns due, un-notified
follow-ups into notifications of kind `application_follow_up`
(`/tracking?tracking_id={id}`), one per record per due date, and stamps
`notified_at`; reading the agenda runs the same sweep, so the list is never stale
by more than the request that opened it. The reminder body names the last
*recorded* status together with its origin, because a reminder must not imply the
product knows something nobody told it.

## Snapshots

When a record opens it freezes what the decision was made with: the match score,
band, explanation and match id, plus the artifact kind, id, version and label.
Both ids are plain integers, not foreign keys — a match result can be re-scored
and a packet can be deleted without breaking the timeline, and the snapshot keeps
answering "which version of my resume got this interview?".

A snapshot is refreshed only explicitly, and the refresh appends an event
carrying the old and new values. It is never refreshed implicitly: silently
moving a score under a historical record would make the report unanswerable.

## Reporting

`GET /api/application-tracking/report?group_by=…` groups by **source**, **role**,
**match band**, **score bucket**, **artifact version**, **artifact kind**,
**channel** and **state**. The rules that make the numbers mean something:

- The denominator is `applied_at IS NOT NULL`. A record that never went out is
  not a failed application.
- `interviews` counts records that reached `interview_scheduled`,
  `interview_completed` or `offer_received`.
- Rates are `null` when the denominator is zero — rendered `—`, never `0%`.
- Every group carries `by_origin`, so a source whose interviews are all
  self-reported is visible as such.
- The window uses `coalesce(applied_at, created_at)`, so a record opened today
  about an application sent last month lands in the month it was sent.

`GET /api/analytics/performance` reads the same timeline now: it reports
`interview_data: "application_tracking"`, and the frontend dropped the "(est)"
label because the number is recorded rather than modelled.

The report ships its own disclaimer, and the UI shows it: *counts come from the
tracking timeline; an interview is only counted when you recorded one; nothing is
inferred from email; a match score is an estimated fit, not an interview
probability.*

## Where it shows up

| Surface | Route | What it does |
|---|---|---|
| Tracking page | `/tracking` | board by state (columns from the server's `counts`), record detail with the full timeline, the follow-up agenda, and the conversion report |
| Job drawer panel | `/jobs` → a job | "Track this application": current state, the one-click interview button, the note box, snapshot chips, and the timeline |
| Analytics | `/analytics` | application→interview conversion by source and by role, from the timeline |
| Notifications | bell | `application_follow_up` reminders linking straight to the record |

Deep links work both ways: `/tracking?tab=follow-ups&tracking_id=7` opens that
record on the board, and every button writes the URL once, so the address bar is
always shareable.

## API

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/application-tracking` | the board (paged, filtered, with column counts) |
| `GET` | `/api/application-tracking/{id}` | one document: state, timeline, transitions, actions |
| `GET` | `/api/application-tracking/{id}/events` | the timeline alone, paged by sequence |
| `GET` | `/api/application-tracking/job/{job_id}` | the document for a job, or `exists: false` |
| `GET` | `/api/application-tracking/follow-ups` | the agenda (and it runs the notification sweep) |
| `GET` | `/api/application-tracking/report` | conversion grouped by any of the eight keys |
| `GET` | `/api/application-tracking/meta` | the vocabulary, for a client that renders forms from the server |
| `POST` | `/api/application-tracking` | open a record for a job |
| `POST` | `…/{id}/status`, `…/interview`, `…/interview-complete`, `…/response`, `…/rejection`, `…/offer`, `…/withdraw` | record what happened |
| `POST` | `…/{id}/note`, `…/follow-up`, `…/follow-up/complete`, `…/attribution` | the non-state facts |
| `POST` | `…/{id}/correction` | amend a belief (reason required) |
| `POST` | `…/{id}/snapshot` | refresh the frozen score/artifact |
| `POST` | `…/{id}/suggestion/confirm`, `…/suggestion/dismiss` | resolve a provisional, untrusted proposal |

Every write returns `{ok, duplicate, state_changed, event, events, document,
state, phase, server_time}` — the document it changed plus what it emitted
([`docs/contracts/13` §8](contracts/13-api-response-shapes.md)).

## What v1 deliberately does not do

No contacts or companies. No email ingestion. No per-employer pipelines or
stages. No offer tracking beyond "an offer exists". No inference — of interviews,
of outcomes, or of anything else from a score. No editing history. Each of those
is a real feature; none of them is what makes the numbers trustworthy, and the
first version had to earn that first.

## Tests

`backend/tests/test_application_tracking.py` (64 tests) covers legal and illegal
transitions, corrections, duplicate and replayed events, origin trust, snapshots,
follow-up notification and completion, the board projection (including the rule
that never downgrades a status the queue owns), report grouping and
denominators, per-tenant isolation, and deletion integrity for both tables. The
frontend surface is covered by `frontend/src/lib/__tests__/tracking.test.ts`,
`frontend/src/components/__tests__/TrackingPanel.test.tsx`,
`frontend/src/pages/__tests__/Tracking.test.tsx` and
`frontend/src/pages/__tests__/Analytics.test.tsx`;
`backend/tests/test_frontend_api_contract.py` checks every tracking call the SPA
makes against the live OpenAPI schema.
