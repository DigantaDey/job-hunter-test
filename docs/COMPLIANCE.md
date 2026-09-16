# Compliance notes (GDPR / CAN-SPAM and platform terms)

This document describes what the software **does** enforce and what remains an operator decision.
It is engineering documentation, not legal advice — a lawyer should review your specific use.

---

## 1. Personal data the system processes

| Category | Where it lives | Notes |
|---|---|---|
| Account data (email, name, password hash) | `users` | password hashed with bcrypt-SHA256, never logged |
| Resume, parsed profile, layout | `resumes`, `profiles`, files under `UPLOAD_DIR`/`GENERATED_DIR` | user-owned |
| Job/application history | `jobs`, `job_events`, `pipeline_jobs` | user-owned |
| Portal credentials | `vault_entries` | encrypted with a per-user key derived from `ENCRYPTION_KEY` |
| Third-party contacts (name, email, title) | `emails`, `email_opt_outs` | see §3 |
| Email engagement (opens, bounces) | `email_events`, `emails.opens` | directional only |
| Audit trail | `audit_logs` | who did what, when, from where |
| Settings & secrets (SMTP password, AI keys) | `settings` | secrets encrypted |

Data is scoped per user: every row carries `user_id`, and access checks are enforced in the API and
covered by tests.

## 2. Data-subject rights (built in)

| Right | Implementation |
|---|---|
| Access / portability | `GET /api/account/export` — one JSON document covering **every table the schema declares as owned**: account, profile, personas, resumes, jobs, job events, the vault (decrypted for the owner), emails and their events, suppressions, application input requests, interview prep, funding companies/scans and company research, settings (SMTP password masked), pipeline and scheduler history, notifications, subscription/billing/usage/credit-ledger rows, error logs and the audit trail. Collections are read in chunks and streamed, so a large account does not make the export fail. Also downloadable from the UI (**Account → Download JSON export**) |
| Erasure | `DELETE /api/account?confirm=<email>` — hard-deletes every owned row **and** the account's files on disk: resume uploads, generated documents with their rendered PDFs, and the autofill screenshots left behind by submitted applications. Which tables are "owned" is read from the schema at runtime (see `app/services/erasure.py`), so a table added later cannot be left behind; files are removed only *after* the transaction commits, what the transaction left behind is counted and logged, and a database refusal is a `409` with the account intact rather than a half-deleted one. An audit row recording the deletion is retained with the user id detached (legitimate-interest record of the erasure itself). |
| Consent | Four explicit consents (terms, automation, outreach, data processing) with timestamped accept/revoke; automation and outreach are blocked without the matching consent. |
| Rectification | Profile, resumes, settings and email drafts are user-editable; generated resumes require approval before use. |
| Objection to tracking | Open tracking is per-instance configurable (`EMAIL_TRACKING_ENABLED`); unsubscribe is honoured immediately and permanently. |

Retention is operator-controlled: delete accounts on request, and set your own retention policy for
`error_logs` and `audit_logs` (both are prunable SQL tables; the app never deletes them on its own).

## 3. Outbound email (CAN-SPAM / GDPR)

Enforced by the compliance gate before any send:

* **Consent** — the sending user must have accepted the outreach disclosure, and the account must
  have accepted the terms.
* **Suppression list** (`email_opt_outs`) — checked for every recipient on every send; hard
  bounces and unsubscribe clicks are added automatically.
* **Syntax + MX verification** — invalid addresses are blocked; disposable domains score 0.
* **Daily limit** — per user, per rolling day (`EMAIL_DAILY_LIMIT` / per-user override).
* **Postal address** — a physical address is required for real sends (CAN-SPAM §5(a)(5)).
* **Unsubscribe** — one-click `GET/POST /api/track/unsubscribe/{token}` plus a `List-Unsubscribe`
  header and footer link in every real message; the link stays valid forever.
* **Dry-run default** — `EMAIL_SENDING_ENABLED=false` and `EMAIL_DRY_RUN=true` until the operator
  deliberately changes them; dry runs are recorded but do not transmit.
* **Honest reporting** — 53x auth failures surface as "needs OTP/app-password", refusals as
  `bounced`; the app never claims a send it did not perform.

Operator duties: lawful basis for contacting each person (B2B legitimate interest is not
automatic), accurate sender identity, honouring opt-outs within the statutory window, and
maintaining SPF/DKIM/DMARC for the sending domain.

## 4. Platform terms of service

* Linkedin, Indeed, Naukri and Instahyre are reported as **gated** — the app does not scrape them.
* Public, documented APIs and public ATS boards (Greenhouse, Lever, Ashby, Workable,
  SmartRecruiters, Workday, Remotive, Arbeitnow, Jobicy, RemoteOK, Himalayas, The Muse,
  WeWorkRemotely) are queried politely: `robots.txt` is honoured (`RESPECT_ROBOTS_TXT=true`), a
  minimum interval per host is enforced, responses are cached, and no authentication is bypassed.
* Automated **application submission** is off by default. The automation consent text states
  plainly that some portals prohibit automated submissions and that accounts may be limited as a
  result. Enabling `AUTOFILL_ENABLED`/`AUTOFILL_ALLOW_SUBMIT` is an operator decision with that
  risk accepted.
* Funding data from SEC EDGAR is public-domain; Crunchbase/Tracxn/imported feeds require your own
  licence, and synthetic demo data is opt-in and labelled `DEMO DATA`.

## 5. Security-of-processing controls

Encryption at rest for credentials and API keys, per-user key derivation, transport via your TLS
terminator, rate limiting, request ids, structured logs, audit trail, least-privilege container
user, and no third-party analytics. See `docs/SECURITY.md` for the full model.

## 6. Before you open the doors — a checklist for counsel

1. Terms of service and privacy policy published, with the lawful basis for each processing purpose.
2. DPA in place with every sub-processor you enabled (AI provider, email provider, contact-data
   provider, hosting).
3. Transfer mechanism for data leaving your jurisdiction (SCCs/adequacy) where applicable.
4. Retention schedule documented (accounts, logs, audit, backups) and enforced.
5. DSAR runbook: who answers, within which statutory window, and how the export/delete endpoints
   are used to fulfil it.
6. Whether automated application submission is offered at all, and if so with what disclosures.
7. Incident-response contact and a breach notification procedure (72 hours under GDPR).
