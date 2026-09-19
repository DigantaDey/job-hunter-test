# Search-engine job discovery

Search supplements direct source adapters; it is **not** a job-details authority.
It is disabled by default and follows the discovery run's `live_enabled` switch.

## Provider evaluation (2026-09-19)

- **Brave — initial production adapter.** Official Web Search and LLM Context
  endpoints are offered. The published Search price is $5/1,000 requests (50 QPS
  capacity, $5 monthly credits). We use **Web Search only**: LLM Context would
  introduce unnecessary content retrieval when only discovery URLs are needed.
  Provider credits are not subtracted from our estimates. Brave explicitly says
  storing results requires a plan with storage rights; confirm your contract
  before enabling this feature. [1](https://brave.com/search/api/)
- **DuckDuckGo — evaluated, not implemented.** The Instant Answer API is not a
  full web-results API; unofficial SERP scrapers are not a suitable production
  dependency for this feature. No DuckDuckGo scraping fallback is installed.
  [2](https://link.sc/blog/duckduckgo-search-api-guide)
- **Google Custom Search — not implemented.** Google's documentation confirms
  the JSON API is closed to new customers and existing customers must transition
  by January 1, 2027. This implementation has no Google dependency.
  [3](https://developers.google.com/custom-search/v1/overview)

Brave's `freshness` parameter filters page age, **not verified job-posting age**.
`page_age` means published/last-modified page date, not our retrieval time or a
confirmed job `datePosted`.
[4](https://api-dashboard.search.brave.com/api-reference/web/search/get)

## Enable

Apply Alembic migrations (`alembic upgrade head` from `backend`, or normal
`AUTO_MIGRATE=true` startup), then configure:

```dotenv
JOB_SEARCH_PROVIDERS=brave
BRAVE_SEARCH_API_KEY=<set through your deployment secret manager>
JOB_SEARCH_STORAGE_RIGHTS=true
```

`JOB_SEARCH_STORAGE_RIGHTS` is an operator attestation that the configured
provider contract permits caching, cross-user reuse of public results, and
persisting attribution. Without it, discovery reports `storage_rights_required`
and does not call the provider. API credentials must be on both API and worker
deployments as appropriate. The key is never included in the search report.

The user needs a **current, active normalized CandidateProfile** for the requested
persona. Only its `preferences.target_roles` and `preferences.remote` are read for
query generation. Free-tier discovery does not depend on enabling AI rescoring.
Draft/missing profiles and unsupported roles produce `no_safe_preferences`;
direct adapters continue normally.

### Replacing providers

`SearchProvider` in `app/services/search/providers.py` defines `id`, a per-request
cost estimate, and `search(SearchRequest) -> list[SearchResult]`. Implement that
contract and register the class in `PROVIDERS`; then select it using
`JOB_SEARCH_PROVIDERS`. Comma-separated IDs define ordered failure fallbacks.
Unknown IDs fail closed. Only Brave ships initially; if it fails, direct sources
still run. Provider adapters must make **one attempt per call** (no hidden
retries), omit snippets, and never accept a user/profile/resume argument.

## Privacy and trust boundary

- Queries use a small curated public role taxonomy, an optional curated seniority
  prefix, and the remote preference. Arbitrary role strings are **not** sanitized
  and forwarded: they are omitted entirely. Extend `queries.ROLES` to support
  additional public role labels. This intentionally trades recall for privacy.
- Names, email, phone, street/city, work history, compensation, eligibility,
  evidence, keywords and resume bodies never enter provider requests. No user ID
  is sent. The same safe preferences produce identical queries across users.
- Provider titles/snippets are discarded. A `DiscoveryLead` starts with
  `status=discovery_lead`; only source-page validation changes it to `validated`.
  Unvalidated leads live in the discovery report, **never** in scored `jobs` rows.
- URLs are canonicalized (tracking parameters/fragments removed, job identifiers
  retained). Invalid schemes, credentials, private literals, service ports, and
  explicitly gated boards are rejected. DNS/public-URL checks and the shared
  HTTP transport guard apply; redirects are followed manually with checks and
  robots compliance on each page hop. No browser login or challenge bypass.
- One page budget covers both result URLs and one level of career-page links.
  Only same-site job links and known ATS links are followed. This is not a
  recursive crawler. Already-fetched direct-source URLs are skipped.
- `WebPageSource` requires a fetched HTML page containing a structured
  `schema.org/JobPosting` with title, hiring organization and nonempty description.
  It emits the existing normalized `Posting` shape. Expired/malformed jobs and
  postings older than the requested window are rejected. Pages without structured
  data, including JS-only pages, remain leads; there is no snippet/LLM fallback.
  Validation means source-page corroboration, not certification of the employer.

## Budgets, caching and failure behavior

Defaults (all configurable in `.env.example`):

| Setting | Default | Meaning |
| --- | ---: | --- |
| `JOB_SEARCH_QUERIES_PER_RUN` | 3 | Query plans and total paid attempts, including fallbacks |
| `JOB_SEARCH_RESULTS_PER_QUERY` | 10 | Max result URLs per query (maximum 20) |
| `JOB_SEARCH_USER_RPM` / `GLOBAL_RPM` | 6 / 30 | Per-user/global requests per UTC minute |
| `JOB_SEARCH_USER_DAILY` / `GLOBAL_DAILY` | 30 / 1000 | Per-user/global requests per UTC day |
| `JOB_SEARCH_CACHE_SECONDS` | 3600 | Shared successful/empty-result cache TTL |
| `JOB_SEARCH_FETCH_LIMIT` | 12 | Source pages fetched per run (redirects capped separately) |
| `JOB_SEARCH_TIMEOUT_SECONDS` | 15 | Provider attempt and per-page timeouts |

Quota counters and cache leases are database-backed, shared by API/worker
processes. All four quota increments are atomic and reserved before an outbound
attempt. A zero quota disables new provider calls. Fixed UTC windows are not
sliding-window throttles. Shared HTTP host politeness provides additional pacing.

The cache key includes provider, query, freshness, result count and query schema
version, not a user ID. It stores only result URLs, provider page dates and the
original retrieval time. Empty results are cached; errors use a 30-second
negative cache. Cache hits do not consume provider-request budgets or cost.
Cached URLs are **revalidated at their source** on each run. Cache TTL does not
extend on hits and cached retrieval dates are not rewritten.

A unique key and 60-second lease prevent concurrent duplicate searches across
workers. A second caller encountering an active lease reports `coalesced` and
continues with its other/direct results instead of paying for a fallback; a
subsequent run can reuse the completed cache. Abandoned leases are reclaimable,
and old owners cannot overwrite newer results. Expired cache/budget records are
reaped during searches. Budget exhaustion never triggers extra paid fallbacks.

Search failure, missing configuration, rate limiting or unsupported pages do not
stop direct-source discovery. Provider error reports contain classified codes,
never raw exception text or response bodies.

## Attribution, freshness and cost

Reports expose `search.attempts`, `cache_hits`, `coalesced`, `errors`,
`budget_exhausted`, `pages_fetched`, `validated`, and the lead list. Attribution
contains provider, query hash, result URL, retrieval timestamp, provider-reported
page date (or `unknown` freshness), and career parent URL where applicable.
Validated jobs retain this under `extra.search_discovery` and in `raw_payload`.
The job's `posted_at` comes **only from its source page**, never from a snippet,
provider page date or the current time. Missing source dates remain unknown.

`search_usage` is a separate ledger, not `ai_credit_ledger`. Each reserved attempt
records provider, initiating user, query hash, request count, outcome and integer
`estimated_cost_microusd`. The default 5,000 micro-USD is $0.005/request; override
`BRAVE_SEARCH_COST_MICROUSD` for your contract. Failed attempts are conservatively
included; these estimates are **not confirmed billed usage**. Cache hits cost
zero. A killed worker may leave `outcome=started`; reconcile those and estimates
against provider usage/invoices. No search costs are charged as model tokens or
AI credits. Usage/budget rows participate in account erasure via their user FK.

Example operator accounting (USD):

```sql
SELECT provider, outcome, SUM(requests) AS requests,
       SUM(estimated_cost_microusd) / 1000000.0 AS estimated_usd
FROM search_usage
GROUP BY provider, outcome;
```

## Tests

```sh
cd backend
pytest tests/test_search_discovery.py tests/test_deletion_integrity.py
```

Provider responses, DNS and source pages are mocked. No test requires a search
key or makes a live search request. Tests cover privacy, source validation,
career links, SSRF/redirects, provenance persistence, expiry, failure fallback,
shared caches/leases, quota races, cost accounting, erasure and migration rollout.
