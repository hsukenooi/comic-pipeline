# Metron API call convention (BUI-262)

**This is the one canonical way every `/comic:*` skill (and Python caller)
talks to Metron (metron.cloud).** Do not hand-roll a raw `curl -u ...` against
metron.cloud, and do not restate "walk `next` until null" as prose — a skill
doc following that prose has no retry, no backoff, no `Retry-After` handling,
and no rate-limit-header awareness, and an agent following it got throttled.
Route through the shared helper instead, mirroring the comics-server
convention ([`docs/conventions/comics-server-call.md`](comics-server-call.md),
BUI-172):

- **Shell / skill callers** — [`scripts/metron-curl.sh`](../../scripts/metron-curl.sh)
- **Python callers** — [`packages/locg-cli/src/locg/metron.py`](../../packages/locg-cli/src/locg/metron.py)
  (the `mokkari`-backed `MetronClient`)

## Metron's documented rules

Source: https://metron-project.github.io/blog/api-best-practices

- **Two rate-limit windows:** burst (20 requests/min) and sustained
  (5,000 requests/day), surfaced on every response via the
  `X-RateLimit-Burst-Remaining` / `X-RateLimit-Sustained-Remaining` headers.
- **Auth** is HTTP Basic Auth via the `Authorization` header
  (`METRON_USERNAME` / `METRON_PASSWORD`).
- **Retry only on 429 and 5xx.** A 429 must sleep the *exact* `Retry-After`
  value the response sends, not a guessed interval. A 5xx retries with
  exponential backoff starting at 1s, capped at 60s. Any other 4xx (400, 401,
  404, ...) means the request itself is wrong — retrying it just repeats the
  mistake, so it must fail immediately, not retry.
- **Pagination is sequential, never parallel.** Walk a list endpoint's `next`
  URL one call at a time. Fetching pages concurrently multiplies burst
  consumption and can blow the 20/min window on a single series lookup.
- **Prefer server-side filtering over client-side loops.** Use Metron's own
  query params (e.g. `year_began`, `series_id`) to narrow a result set instead
  of fetching everything and filtering per-item locally — see BUI-204 in
  `.claude/commands/comic/wishlist-add.md` for why an unfiltered `X-Men` search
  costing 327 rows was the single biggest token cost in that skill.

## How a skill uses it

```bash
source "$(git rev-parse --show-toplevel)/scripts/metron-curl.sh"

# A single call
metron_curl "https://metron.cloud/api/series/?name=Thor" || exit 1

# A paginated list — walks `next` sequentially, yields one JSON result per line
metron_paginate "https://metron.cloud/api/issue/?series_id=123" | while IFS= read -r issue; do
  number="$(printf '%s' "$issue" | jq -r '.number')"
  ...
done
```

## The two functions

- **`metron_curl <url> [extra curl args...]`** — wraps `curl -u
  "$METRON_USERNAME:$METRON_PASSWORD"`. Retries a 429 by sleeping the response's
  `Retry-After` header exactly; retries a 5xx with exponential backoff (1s →
  60s cap); fails loudly (non-zero exit, error body on stderr) on any other
  non-2xx, with no retry. Stdout carries the response body on success. Both
  the retry ceiling (`METRON_CURL_MAX_RETRIES`, default 6) and the per-request
  timeout (`METRON_CURL_MAX_TIME`, default 30s) are overridable env vars.
- **`metron_paginate <url>`** — walks a paginated Metron list endpoint's
  `next` field sequentially (never parallel) via repeated `metron_curl` calls,
  emitting each page's `results` entries as newline-delimited JSON. Stops on
  its own once `next` is null — no manual loop-until-null logic needed in the
  caller. Caps at `METRON_PAGINATE_MAX_PAGES` (default 500) so a malformed
  `next` chain can't loop forever.

Both functions log the remaining `X-RateLimit-Burst-Remaining` /
`X-RateLimit-Sustained-Remaining` budget to stderr on every response that
carries those headers, so an agent burning through a large batch (e.g. a
685-issue wish-list run) can see it approaching the limit before a 429 hits.

## The rule that matters most

**Never retry a non-429/5xx failure, and never guess a retry interval.** A
400/401/404 means something about the request is wrong (bad series id, bad
credentials, wrong path) — retrying it burns rate-limit budget without ever
succeeding. And guessing a backoff instead of honoring `Retry-After` risks
retrying into the same window Metron just told the caller to wait out.
