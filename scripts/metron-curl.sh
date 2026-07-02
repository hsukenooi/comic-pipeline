#!/usr/bin/env bash
# BUI-262: the ONE canonical Metron API call convention for the /comic:* skills.
#
# .claude/commands/comic/wishlist-add.md used to instruct an agent to hand-roll
# `curl -u "$METRON_USERNAME:$METRON_PASSWORD" ...` and "walk `next` until
# null" in prose — zero code enforcement, no retry, no backoff, no
# Retry-After handling, no rate-limit-header awareness. An agent following
# that prose got throttled. This is the single definition every skill that
# talks to Metron routes through, mirroring the comics-server convention
# (BUI-172, scripts/comics-server.sh). Source it, then:
#
#     source "$(git rev-parse --show-toplevel)/scripts/metron-curl.sh"
#     metron_curl "https://metron.cloud/api/series/?name=Thor" || exit 1
#     metron_paginate "https://metron.cloud/api/issue/?series_id=123" | while read -r item; do
#       number="$(printf '%s' "$item" | jq -r '.number')"
#       ...
#     done
#
# Metron's documented best practices
# (https://metron-project.github.io/blog/api-best-practices):
#   - two rate-limit windows: burst 20 req/min, sustained 5,000/day, surfaced
#     via the X-RateLimit-Burst-Remaining / X-RateLimit-Sustained-Remaining
#     response headers.
#   - auth is Basic Auth via the Authorization header.
#   - retry ONLY on 429 (honor Retry-After exactly) and 5xx (exponential
#     backoff, 1s start, 60s cap); never retry any other 4xx — those mean the
#     request itself is wrong, not transient.
#   - pagination via `next` must be walked SEQUENTIALLY, never in parallel.
#
# See docs/conventions/metron-api-best-practices.md for the full rules and the
# Python-caller equivalent (packages/locg-cli/src/locg/metron.py).
#
# Credentials: METRON_USERNAME / METRON_PASSWORD, normally sourced from
# ~/.config/locg/.env (see .claude/commands/comic/wishlist-add.md Step 0).

# Extract a header value from a curl -D dump. Header names are matched
# case-insensitively (grep -i) since servers are not required to preserve
# casing; CRLF line endings are stripped.
_metron_header_value() {
  local headers_file="$1" name="$2"
  grep -i "^${name}:" "$headers_file" | tail -n 1 | sed -E 's/^[^:]+:[[:space:]]*//' | tr -d '\r\n'
}

# Log the remaining rate-limit budget (when the response carried the
# headers) to stderr so an agent burning through a batch can see it's
# approaching the limit before getting 429'd.
_metron_log_rate_limit() {
  local headers_file="$1" burst sustained
  burst="$(_metron_header_value "$headers_file" "X-RateLimit-Burst-Remaining")"
  sustained="$(_metron_header_value "$headers_file" "X-RateLimit-Sustained-Remaining")"
  if [ -n "$burst" ] || [ -n "$sustained" ]; then
    echo "metron_curl: rate-limit remaining — burst=${burst:-?} sustained=${sustained:-?}" >&2
  fi
}

# The hard-failing call wrapper for a single Metron URL. Retries 429
# (sleeping the exact Retry-After header value) and 5xx (exponential
# backoff, 1s start, 60s cap); any other non-2xx fails loudly and immediately
# — a 4xx other than 429 means the request itself is wrong, retrying it would
# just repeat the mistake. Stdout carries the response body on success.
METRON_CURL_MAX_TIME="${METRON_CURL_MAX_TIME:-30}"
METRON_CURL_MAX_RETRIES="${METRON_CURL_MAX_RETRIES:-6}"
metron_curl() {
  if [ -z "${METRON_USERNAME:-}" ] || [ -z "${METRON_PASSWORD:-}" ]; then
    echo "metron_curl: METRON_USERNAME/METRON_PASSWORD are not set." >&2
    return 1
  fi

  local url="$1"
  shift || true
  local attempt=0 backoff=1
  # NOTE: named http_status, not "status" — zsh reserves "status" as a
  # read-only alias for $?, and comic-pipeline skills are sourced under both
  # bash and zsh (this repo's default shell). "local status; status=..."
  # hard-fails under zsh with "read-only variable: status".
  local body_file headers_file http_status retry_after

  while :; do
    body_file="$(mktemp)"
    headers_file="$(mktemp)"
    http_status="$(curl -sS -u "${METRON_USERNAME}:${METRON_PASSWORD}" \
      --max-time "$METRON_CURL_MAX_TIME" \
      -D "$headers_file" -o "$body_file" -w '%{http_code}' "$url" "$@")"

    _metron_log_rate_limit "$headers_file"

    if [ "$http_status" -ge 200 ] && [ "$http_status" -lt 300 ]; then
      cat "$body_file"
      rm -f "$body_file" "$headers_file"
      return 0
    fi

    if [ "$http_status" = "429" ] && [ "$attempt" -lt "$METRON_CURL_MAX_RETRIES" ]; then
      retry_after="$(_metron_header_value "$headers_file" "Retry-After")"
      [ -n "$retry_after" ] || retry_after=1
      echo "metron_curl: 429 rate-limited; sleeping Retry-After=${retry_after}s (attempt $((attempt + 1))/${METRON_CURL_MAX_RETRIES})" >&2
      rm -f "$body_file" "$headers_file"
      # Assumes Retry-After is the numeric-seconds form, which is what Metron
      # sends. `sleep` would reject the alternate HTTP-date form (e.g. "Wed,
      # 21 Oct 2026 07:28:00 GMT"); that form is not handled — a known,
      # accepted limitation rather than a general RFC 7231 implementation.
      sleep "$retry_after"
      attempt=$((attempt + 1))
      continue
    fi

    if [ "$http_status" -ge 500 ] && [ "$http_status" -lt 600 ] && [ "$attempt" -lt "$METRON_CURL_MAX_RETRIES" ]; then
      echo "metron_curl: ${http_status} server error; backing off ${backoff}s (attempt $((attempt + 1))/${METRON_CURL_MAX_RETRIES})" >&2
      rm -f "$body_file" "$headers_file"
      sleep "$backoff"
      attempt=$((attempt + 1))
      backoff=$((backoff * 2))
      [ "$backoff" -gt 60 ] && backoff=60
      continue
    fi

    # Anything else — including http_status "000", which curl -w reports for a
    # TOTAL failure (DNS lookup, connection refused, --max-time timeout, no
    # HTTP response at all) — falls through here and fails without retry.
    # Deliberately conservative: we only retry the two codes Metron's docs
    # call out as transient (429, 5xx); a network blip that never reached the
    # server is surfaced immediately rather than silently retried.
    echo "metron_curl call FAILED (HTTP ${http_status}): ${url}" >&2
    [ -s "$body_file" ] && cat "$body_file" >&2
    rm -f "$body_file" "$headers_file"
    return 1
  done
}

# Walk a paginated Metron list endpoint SEQUENTIALLY (never parallel) via
# repeated metron_curl calls, following each page's `next` until it is null.
# Yields each page's `results` entries as newline-delimited JSON on stdout
# (one item per line), so a caller loops with:
#
#   metron_paginate "$url" | while IFS= read -r item; do
#     number="$(printf '%s' "$item" | jq -r '.number')"
#   done
METRON_PAGINATE_MAX_PAGES="${METRON_PAGINATE_MAX_PAGES:-500}"
metron_paginate() {
  if ! command -v jq >/dev/null 2>&1; then
    echo "metron_paginate: jq is required to walk paginated results." >&2
    return 1
  fi

  local url="$1"
  local page_num=0
  local body

  while [ -n "$url" ] && [ "$url" != "null" ]; do
    page_num=$((page_num + 1))
    if [ "$page_num" -gt "$METRON_PAGINATE_MAX_PAGES" ]; then
      echo "metron_paginate: exceeded ${METRON_PAGINATE_MAX_PAGES} pages — aborting (possible infinite next loop)" >&2
      return 1
    fi
    body="$(metron_curl "$url")" || return 1
    printf '%s\n' "$body" | jq -c '.results[]'
    url="$(printf '%s' "$body" | jq -r '.next // empty')"
  done
}
