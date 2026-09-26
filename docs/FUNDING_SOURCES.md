# Funding sources & the India strategy

How the funding radar gets its events, why it defaulted to the US market, and
the layered plan to cover Indian companies. Code entry points:
`app/services/funding_sources.py` (provider registry + AI passes),
`app/services/funding_search.py` (web-search path), `app/services/funding_radar.py`
(scan orchestration + persistence).

## Why the radar looked "SEC-only"

`FUNDING_PROVIDER` defaults to **`sec_edgar`** for one reason: it is the only
**keyless, official** feed of private-capital disclosures. The US requires a
**Form D** filing for every private placement, the SEC publishes it in a
machine-readable full-text API, and no account or licence is needed. Nothing
else in the provider registry is keyless *and* official:

| Provider | Coverage | Keyless? | What an event is |
|---|---|---|---|
| `sec_edgar` | **US only** (Form D / private placements) | yes | a filing (`verified=True`, links the filing) |
| `search` (Tavily etc.) | **Worldwide**, incl. India | no — needs `FUNDING_SEARCH_API_KEY` | a cited publication |
| `crunchbase` | Global (US-weighted) | no — operator licence | API entity |
| `tracxn` | **Global, India-strong** (vendor is Bengaluru-based) | no — operator licence | API entity |
| `imported` | Whatever the export holds | no — operator-hosted URL | licensed CSV/JSON row |
| `demo` | Synthetic, opt-in only | n/a | labelled `verified=False` |

`auto` (or a comma-less list — one id per scan today) runs every *configured*
provider; the substitution rule in `fetch_funding_events` swaps `sec_edgar`
for `search` whenever a search provider is configured, because the search path
supersedes Form D discovery (public announcements **and** filings, worldwide).

India has **no equivalent of Form D**: the MCA/RoC filings that exist
(PAS-3 allotments, CHG-1 charges, SH-4 transfers) are per-company statutory
documents with no anonymous bulk feed, so "the SEC of India, but free" does not
exist. Indian coverage therefore has to be assembled from layers — from
searchable public disclosures first, licensed registries second, official
filings only where the access rules allow.

## The strategy — five layers

### L0 — region-aware web-search path *(implemented)*

The search path already discovers funding news worldwide; what was missing was
market phrasing and currency:

* **Queries follow the candidate.** When the search context's `locations` or
  `keywords` point at India (Kolkata, Bengaluru, "Bangalore SaaS", …), every
  query is suffixed with " India" and one market-angled query is added
  (`_search_queries` / `_india_hinted` in `funding_sources.py`). Indian
  publications that a US-phrased query never surfaces come back with the
  results.
* **INR amounts are first-class.** The extraction pass converts ₹ / lakh /
  crore amounts to USD at a **fixed approximation (₹88/USD)** used only for
  ordering, never invents another rate (`EXTRACT` prompt in
  `funding_sources.py`).
* Every event still cites the result row it came from — `url` + `snippet`,
  never the model's prose — so an Indian round is exactly as verifiable as a
  US one.

Requires: `FUNDING_SEARCH_PROVIDER=tavily` + `FUNDING_SEARCH_API_KEY`.
Without a key the radar falls back to `sec_edgar` (US Form D) — stated openly
in the UI badge and in `search_hint()`.

### L1 — Indian startup-news feeds *(next)*

A keyless, robots-compliant **feed provider** over RSS/Atom endpoints of the
outlets that publish Indian funding rounds daily (Inc42, Entrackr, Economic
Times startup desk, Mint business, YourStory, and similar):

* feeds are polled with the shared HTTP client (robots respected, SSRF guard,
  per-feed cache TTL = half the scan window), parsed to `{title, url, snippet,
  published_at}` — the exact row shape `funding_search.search` returns;
* the **existing** extraction pass (`ai_extract_events_from_results`) turns
  snippets into events with citations — no new AI surface, no new guardrails;
* verified semantics follow the search path: `verified=True` (a checkable
  publication), `source="india_news"` (or per-feed id) for provenance;
* honest failure: a feed that is down becomes `provider_errors.<feed>` on the
  scan report, never a quiet empty radar (the provider robustness contract in
  `funding_sources`).

Acceptance: with no paid key at all, a scan with an India-hinted context
returns Indian funding events with citations; with the feed host unreachable,
the report says so.

### L2 — licensed registries *(operator-provided)*

Already wired, zero code:

* **Tracxn** — Indian vendor, deepest India private-market coverage; set
  `TRACXN_API_KEY`.
* **Crunchbase** — global; set `CRUNCHBASE_API_KEY`.
* **`imported`** — a licensed CSV/JSON export URL; the documented row schema
  (name/stage/date/website/industry/raised_usd/careers_url/open_positions) is
  how a team publishes its own Tracxn/Crunchbase extract.

Acceptance: `FUNDING_PROVIDER=auto` with either key yields India events
labelled `source=tracxn|crunchbase`, `verified=True`, stage included (these
registries carry the stage Form D never does).

### L3 — official Indian filings *(researched, gated on access)*

The Form D equivalent for India, when an access route exists:

* **MCA21 / RoC filings** — `PAS-3` (return of allotment: a priced round
  happened), `CHG-1` (charge creation: debt), `SH-4` (share transfers).
  No anonymous bulk API; access is via the MCA21 portal's paid/enterprise
  routes or a licensed reseller — any use must stay inside MCA's terms of
  service. An event from this layer links the filing (`verified=True`), stage
  stays `Undisclosed` (an allotment filing names the raise, not the round
  label) — the same honesty rule as Form D.
* **BSE/NSE announcements** — preferential allotments and conversions by
  *listed* companies; the exchange publishes announcements publicly. Narrower
  universe (listed SMEs/large caps), but official and free.
* **SEBI AIF quarterly filings** — fund-level positions, not per-round
  events; useful for verification/enrichment, not for discovery.

Acceptance: any event from this layer carries the filing URL, `verified=True`,
and a `meta.form_type`; nothing is inferred the filing does not say.

### L4 — demo data *(opt-in, labelled)*

`ALLOW_SYNTHETIC_FUNDING_DATA=true` shows the surface with invented companies
(`source="demo"`, `verified=False`, summary prefixed `[DEMO DATA]`). It exists
for demos, never as a substitute for L0–L2, and can be extended with
India-shaped rows without touching real-data contracts.

## Cross-cutting rules

* **Currency.** `raised_usd` is the single numeric field the UI ranks on.
  INR is converted only at the prompt's fixed approximation (L0) or by the
  provider itself (L2/L3 send USD or a documented rate); `raised_at_estimated`
  keeps marking rows whose *day* is an approximation. Never a silent guess:
  a row whose amount cannot be stated honestly reports `null`.
* **Dedupe.** Identity is the normalised company name (`normalize_company_name`),
  so a round reported by a feed *and* a registry is one radar row — the first
  source wins and keeps its citation.
* **Ranking.** Unchanged: `ai_rank_events` may only judge events a provider
  actually returned; fabricated names reject the whole answer.
* **Provenance.** `source` tells the operator which layer produced the row;
  the scan report's `counts`/`errors` stay per-provider, so "no Indian rounds
  this month" and "the India feed is down" remain different answers.

## Phases

| Phase | Deliverable | Status |
|---|---|---|
| P0 | EDGAR RPC fix (keyless default works); region-aware queries; INR conversion; docs | **done** |
| P1 | Feed provider (L1) behind `FUNDING_PROVIDER=…` with per-feed config | next |
| P2 | Operator keys: Tracxn first for India (L2) — no code, docs + UI hints | ready when a key exists |
| P3 | Official-filing research (L3): MCA access route + SEBI/BSE, ToS review first | gated |
| P4 | India-shaped demo rows (L4) if demos need them | optional |
