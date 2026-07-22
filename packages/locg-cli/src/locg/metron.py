"""Thin mokkari wrapper for Metron API series + issue lookup."""
from __future__ import annotations

import functools
import logging
import os
import re
import time
from typing import Any, Optional

from mokkari.exceptions import ApiError, RateLimitError

logger = logging.getLogger("locg")

# Metron enforces 20 req/min burst + 5,000/day sustained and reports a
# ``retry_after`` on RateLimitError. Cap the wait regardless of what Metron
# reports: this client can be called synchronously from inside an async route
# handler on a single-worker server, so an unbounded sleep would wedge it
# (BUI-255 lesson) — a single capped retry, not escalating backoff, is the
# intended behavior (BUI-260).
_RATE_LIMIT_MAX_SLEEP = 60.0

# mokkari's own hardcoded prefix (session.py: `_execute_http_request`) for a
# ``requests`` ``ConnectionError``/``ReadTimeout`` it re-wraps as ``ApiError``
# before it ever reaches us — the underlying timeout exception isn't
# preserved, so this string is the only in-band signal that a failure was a
# transport-level timeout/outage rather than a data-shape or 404 ApiError
# (BUI-255). mokkari's request timeout is a hardcoded 20s
# (``mokkari.session.REQUEST_TIMEOUT``), so this fires at most once per call.
_CONNECTION_ERROR_PREFIX = "Connection error:"

# BUI-342: single capped retry sleep for a Metron 5xx. Metron's best-practices
# doc wants exponential backoff (1s→60s), but this client runs synchronously
# inside the single-worker async route, so — exactly as for the rate-limit path
# (BUI-260) — we do ONE short capped retry, not escalating backoff. Start at the
# doc's 1s floor; a 5xx is rare and transient.
_SERVER_ERROR_RETRY_SLEEP = 1.0

# BUI-465: the burst budget above, and what each public lookup actually SPENDS
# against it. A batch caller that paces itself per *call* under-counts, because
# `lookup_issue` is two HTTP requests (`series_list` then `issues_list`) while
# `lookup_issue_detail` is one (`session.issue`). These live here, next to the
# methods whose request counts they describe, so a lookup that grows a third
# request can't silently leave a caller's pacing maths behind.
#
# BUI-473: `lookup_issue`'s two requests are now individually addressable —
# `REQUESTS_RESOLVE_SERIES` (the `series_list` half, spent by `resolve_series`)
# and `REQUESTS_ISSUE_IN_SERIES` (the `issues_list` half, spent by
# `issue_in_series`) — because `resolve_series` is now genuinely reusable
# across every issue of the same series in a batch (see `MetronClient`'s
# per-instance series cache), while `issue_in_series` never is. A caller that
# still wants the combined, one-call cost (anything still calling plain
# `lookup_issue` without reusing a resolved series) keeps using the composed
# `REQUESTS_LOOKUP_ISSUE`, which is defined FROM the two halves so it can
# never silently drift out of sync with them.
REQUESTS_PER_MINUTE = 20
REQUESTS_RESOLVE_SERIES = 1
REQUESTS_ISSUE_IN_SERIES = 1
REQUESTS_LOOKUP_ISSUE = REQUESTS_RESOLVE_SERIES + REQUESTS_ISSUE_IN_SERIES
REQUESTS_LOOKUP_ISSUE_DETAIL = 1
# BUI-501: `lookup_issue_by_id`'s one request (`session.issue(metron_id)`,
# the SAME call `lookup_issue_detail` makes — see that method's request
# count above this one).
REQUESTS_LOOKUP_ISSUE_BY_ID = 1


# BUI-485: Metron's ``series_list({"name": q})`` is a substring (icontains)
# search anywhere in the name, so "Batman" returns hundreds of series —
# "Absolute Batman", "Tangent Comics / The Batman", "Batman Annual" — that
# the caller never asked for. These two normalize a query and a candidate's
# ``display_name`` for an EXACT-match test (never substring/containment,
# which is exactly the permissiveness this exists to correct):
#   - a trailing " (YYYY)" decoration (Metron's display_name convention) is
#     stripped, e.g. "The Amazing Spider-Man (1963)" -> "The Amazing Spider-Man"
#   - a leading "The"/"A"/"An" article is stripped from BOTH sides, since the
#     query and Metron's display_name may disagree on whether it's present
#     (query "Amazing Spider-Man" must still match display_name
#     "The Amazing Spider-Man (1963)")
#   - casefold for case-insensitive comparison
#
# ``commands.py`` and ``collection_cache.py`` each already have their own
# private series-name normalizer with different stripping rules (LOCG display
# decoration, not Metron's). Named ``_normalize_metron_display_name`` (not the
# generic ``_normalize_series_name`` those modules use) so a cross-module grep
# doesn't turn up three same-named-but-different-behavior functions.
_TRAILING_YEAR_RE = re.compile(r"\s*\(\d{4}\)\s*$")
_LEADING_ARTICLE_RE = re.compile(r"^(?:the|a|an)\s+", re.IGNORECASE)


def _normalize_metron_display_name(name: Any) -> str:
    n = str(name or "").strip()
    n = _TRAILING_YEAR_RE.sub("", n)
    n = _LEADING_ARTICLE_RE.sub("", n)
    return n.strip().casefold()


# BUI-487: an Annual/Giant-Size/Special sometimes ships under a different
# masthead than the parent ongoing series uses for it — e.g. Marvel's own
# indicia says "Uncanny X-Men Annual", but Metron files the book under the
# ongoing's ORIGINAL (pre-"Uncanny") masthead: "X-Men Annual (1970)". The
# BUI-485 exact-name filter above is correct and necessary but not
# sufficient here — it makes a masthead mismatch fail cleanly (zero exact
# survivors -> None), but it can't discover that "Uncanny X-Men Annual" and
# "X-Men Annual" are the same in-universe book. This table is that missing
# link: a plain data mapping from OUR masthead to METRON'S, consulted before
# both the ``series_list`` search and the exact-name filter (see
# ``_map_annual_masthead`` / ``lookup_issue``).
#
# A masthead with NO entry here passes through unchanged — this is the
# ordinary case for the vast majority of series, not a failure. A WRONG or
# missing entry changes only what string gets searched/exact-matched; it does
# NOT add a new way to guess. When ``series_list`` comes back with 2+
# candidates, the existing exact-name-filter pipeline still fails closed
# (None) on a mapped name that doesn't line up with any real candidate —
# reviewed and pinned by tests below. The rare case where a mapped/mistyped
# value collapses Metron's live substring search down to exactly one hit was
# BUI-487's out-of-scope residual: ``_disambiguate_series``'s
# ``len(series_list) == 1`` branch (this file, above) used to trust that SOLE
# candidate UNFILTERED by name or year (a BUI-474/BUI-485 measurement this
# table deliberately did not revisit). BUI-494 tightened that residual — the
# singleton branch now fails closed (None -> needs_manual_series) on a sole
# candidate that neither exact-name-matches the mapped query NOR covers the
# win's year, so a mapped value collapsing to a single OUT-of-era WRONG hit is
# no longer trusted. (An in-era wrong sole hit can still pass the year-window
# acceptor — the lenient OR floor BUI-494 chose to avoid regressing legitimate
# on-era single candidates — so a new table value must still be verified
# against Metron's real display_name to keep even that path honest.) Adding a
# newly discovered divergence is a new key here, never a new branch in
# ``lookup_issue`` or ``_disambiguate_series``.
#
# Sibling precedent: ``collection_cache.py``'s ``_MASTHEAD_ALIAS_PAIRS``
# (BUI-197) is the same shape of table for the LOCG-side collection matcher
# (e.g. "mighty thor" <-> "thor"). This table is intentionally separate —
# it encodes Metron's OWN catalog naming, a distinct source of truth from
# LOCG's catalog spelling — but a new entry here should still be verified
# against Metron's actual ``display_name`` before merging, the same
# discipline that table documents for its own pairs.
#
# Keys are matched via ``_normalize_metron_display_name`` (see
# ``_map_annual_masthead``) — casefolded, with a leading "The"/"A"/"An" and
# any trailing " (YYYY)" stripped, same as the exact-name filter uses on the
# candidate side; values are Metron's ``display_name`` before its own
# trailing " (YYYY)" decoration.
_ANNUAL_MASTHEAD_TO_METRON: dict[str, str] = {
    "uncanny x-men annual": "X-Men Annual",
}


def _map_annual_masthead(series_query: str) -> str:
    """Translate ``series_query`` via ``_ANNUAL_MASTHEAD_TO_METRON``, else pass through.

    Looks the query up by ``_normalize_metron_display_name`` (not a raw
    ``.casefold()``) so a leading article or an incidental trailing year
    decoration on the INCOMING query — e.g. "The Uncanny X-Men Annual" —
    still hits the table entry keyed on the bare masthead; identify_data's
    series naming isn't guaranteed to omit either.

    A miss (no key matches) returns ``series_query`` verbatim: the caller's
    existing substring search + exact-name filter already fail closed on an
    unmapped masthead divergence, so "no mapping found" needs no special
    handling of its own.
    """
    return _ANNUAL_MASTHEAD_TO_METRON.get(
        _normalize_metron_display_name(series_query), series_query
    )


def _is_connection_error(exc: BaseException) -> bool:
    return isinstance(exc, ApiError) and str(exc).startswith(_CONNECTION_ERROR_PREFIX)


def _http_status_from_cause(exc: BaseException) -> Optional[int]:
    """Recover the HTTP status of a mokkari ``ApiError`` from its chained cause.

    A Metron HTTP error surfaces as ``ApiError`` because mokkari's
    ``_handle_http_response`` does ``raise ApiError(msg) from err`` where ``err``
    is the underlying ``requests.exceptions.HTTPError`` (which still carries
    ``.response.status_code``). Reading the status off ``exc.__cause__`` is
    robust across mokkari/requests versions — both the ``from err`` chaining and
    ``HTTPError.response`` are stable API — unlike scraping the message string.
    Data-shape ``ApiError``\\ s (pydantic validation) and ``detail``-based 404s
    are raised WITHOUT ``from`` (or from a non-HTTP cause), so this returns
    ``None`` for them, which is exactly what keeps a genuine no-match from
    tripping the breaker.
    """
    cause = getattr(exc, "__cause__", None)
    response = getattr(cause, "response", None)
    status = getattr(response, "status_code", None)
    return status if isinstance(status, int) else None


def _is_server_error(exc: BaseException) -> bool:
    """True iff ``exc`` is an ``ApiError`` wrapping a genuine HTTP 5xx (BUI-342).

    Deliberately narrow: only 500–599 with a recoverable status trips. A
    data-shape / 404 / connection ``ApiError`` returns False so it stays a silent
    ``None`` (a no-match), never a false "Metron is down" that would disable
    enrichment for the rest of a batch.
    """
    if not isinstance(exc, ApiError):
        return False
    status = _http_status_from_cause(exc)
    return status is not None and 500 <= status < 600


def _retry_once_on_rate_limit(func):
    """Decorator: on ``RateLimitError``, sleep (capped) and retry the call ONCE.

    Every public lookup below used to catch ``RateLimitError`` with the same
    blanket ``except Exception`` used for a genuine "no match found," so a
    throttled call and an absent comic were indistinguishable — the caller
    silently got ``None`` either way and never actually asked Metron (BUI-260).
    This decorator intercepts ``RateLimitError`` before that blanket handler,
    logs at ``warning`` (a rate-limit event is not a routine miss), waits
    ``min(exc.retry_after, _RATE_LIMIT_MAX_SLEEP)``, and retries once. Only a
    second ``RateLimitError`` — from the retry itself — falls through to
    ``None``.

    Also maintains ``self.degraded`` (BUI-255): reset to ``False`` at the
    start of every decorated call, then flipped ``True`` only by a failure
    path — here on an exhausted rate-limit retry or an exhausted 5xx retry
    (BUI-342), or inside the method body itself on a connection-error
    ``ApiError`` (see ``_is_connection_error``). A batch caller
    (``cmd_collection_record_win``) polls this after a ``None`` result to tell
    "Metron is throttled/unreachable/erroring, stop calling it for the rest of
    the batch" apart from a genuine, exception-free "no match" — which never
    touches the flag and leaves it at the optimistic reset.

    5xx handling (BUI-342): a genuine server error surfaces from the method
    bodies as a re-raised ``ApiError`` (they swallow every OTHER ``ApiError`` —
    data-shape, 404, connection — and return ``None``; only ``_is_server_error``
    re-raises). It gets ONE short capped retry here, symmetric with the
    rate-limit path. Both retries' inner handlers catch ``(RateLimitError,
    ApiError)`` so a retry that fails with the *other* transient class (5xx
    after a 429, or vice-versa) still trips ``degraded`` rather than escaping
    the decorator and crashing the batch.
    """
    @functools.wraps(func)
    def wrapper(self, *args, **kwargs):
        self.degraded = False
        try:
            return func(self, *args, **kwargs)
        except RateLimitError as exc:
            wait = min(max(exc.retry_after, 0), _RATE_LIMIT_MAX_SLEEP)
            logger.warning(
                "Metron rate limit hit in %s; retrying once after %.1fs",
                func.__name__, wait,
            )
            time.sleep(wait)
            try:
                return func(self, *args, **kwargs)
            except (RateLimitError, ApiError):
                logger.warning(
                    "Metron still throttled/erroring in %s after rate-limit "
                    "retry; giving up", func.__name__,
                )
                self.degraded = True
                return None
        except ApiError as exc:
            # Only a genuine 5xx reaches here — see _is_server_error and the
            # method-body handlers, which re-raise 5xx and swallow all other
            # ApiErrors as None. Guard anyway: a non-5xx ApiError that somehow
            # propagated is a plain failure, so mirror the body and return None
            # WITHOUT tripping the breaker (a data-shape error is not an outage).
            if not _is_server_error(exc):
                logger.debug(
                    "Metron non-5xx ApiError propagated to retry wrapper in %s: %s",
                    func.__name__, exc,
                )
                return None
            logger.warning(
                "Metron 5xx in %s; retrying once after %.1fs",
                func.__name__, _SERVER_ERROR_RETRY_SLEEP,
            )
            time.sleep(_SERVER_ERROR_RETRY_SLEEP)
            try:
                return func(self, *args, **kwargs)
            except (RateLimitError, ApiError):
                logger.warning(
                    "Metron 5xx again in %s after retry; giving up",
                    func.__name__,
                )
                self.degraded = True
                return None
    return wrapper


class MetronCredentialError(RuntimeError):
    """Raised on first use when METRON_USERNAME or METRON_PASSWORD is absent."""


class MetronClient:
    """Lazy mokkari wrapper. The mokkari session is created on first use."""

    def __init__(self) -> None:
        self._session: Any = None
        # BUI-255: True after the most recent call failed because Metron is
        # throttled (rate-limit retry exhausted) or unreachable (connection
        # timeout) — as opposed to a genuine, exception-free "no match". A
        # batch caller checks this to disable Metron for the rest of a batch
        # instead of retrying (and sleeping) on every remaining row.
        self.degraded = False
        # BUI-473: per-instance series-resolution cache, keyed by
        # ``(masthead-mapped series_query, coerced year)`` -> the
        # `_disambiguate_series` result for that key (a series handle, or
        # ``None`` for a genuine no-match/ambiguous result — see
        # `resolve_series`). One `MetronClient` lives for the whole of a
        # record-win batch (`cmd_collection_record_win` constructs or is
        # handed exactly one), so this makes a run of N issues from the same
        # series spend the `series_list` request ONCE rather than N times,
        # without either `lookup_issue`'s callers or its return shape
        # changing. Deliberately NOT a cache on `lookup_issue_detail` (keyed
        # by per-issue `metron_id`, which is all-distinct within one series —
        # BUI-465 already measured that shape gets zero hits); BUI-473 is
        # scoped to the series half only.
        self._series_cache: dict[tuple[str, Optional[int]], Optional[Any]] = {}

    def _get_session(self) -> Any:
        if self._session is not None:
            return self._session

        username = os.environ.get("METRON_USERNAME")
        password = os.environ.get("METRON_PASSWORD")
        if not username or not password:
            raise MetronCredentialError(
                "METRON_USERNAME and METRON_PASSWORD must be set in "
                "~/.config/locg/.env or the environment to use Metron lookup."
            )

        import mokkari
        self._session = mokkari.api(username, password, user_agent="locg-cli/1.0")
        return self._session

    @staticmethod
    def _disambiguate_series(
        series_list: list[Any], year: Any, query: str
    ) -> Optional[Any]:
        """Pick the series whose publication range includes ``year`` (BUI-32).

        - exactly one candidate -> trust it ONLY IF it clears a name/year gate
          (BUI-494): accept iff its ``display_name`` EXACT-matches ``query``
          (``_normalize_metron_display_name``, BUI-485) OR ``year`` falls
          inside its ``[year_began, year_end]`` window (BUI-32); otherwise
          ``None`` so the caller fails closed to ``needs_manual_series``. A
          null ``year`` (or a candidate with no ``year_began``) can't evaluate
          the window, so the sole candidate is then trusted on an exact-name
          match alone. This OR is deliberately MORE LENIENT than the
          multi-candidate branch below (which requires exact-name AND a unique
          in-window survivor) — the leniency keeps the genuine
          single-and-on-era BUI-474 population resolving instead of regressing
          it to manual review. (Previously the sole candidate was trusted
          UNFILTERED by name or year: BUI-474 measured that population had no
          wrong picks, but BUI-487's masthead mapping can collapse Metron's
          substring search to a single WRONG hit. The gate now fails closed on
          such a collapse when the wrong hit is OUT of era; an in-era wrong
          sole hit still slips through the year-window acceptor — the accepted
          floor of the OR, since requiring exact-name here would regress
          legitimate on-era single candidates.)
        - otherwise, first narrow to candidates whose ``display_name``
          EXACT-matches ``query`` (BUI-485; see ``_normalize_metron_display_name``)
          — Metron's search is a substring match, so multiple candidates is not
          evidence any of them are actually "on-topic" (e.g. "Batman" also
          returns "Absolute Batman" and the Annual sibling, which a year
          window alone can never separate since they share the run)
        - then, of the exact-name survivors, ``year`` falling in exactly one
          ``[year_began, year_end]`` range (year_end ``None`` means ongoing)
          -> that one
        - otherwise (no year, no exact-name survivor, or still ambiguous)
          -> ``None`` so the caller falls back to ``needs_manual_series_canonical``
        """
        query_norm = _normalize_metron_display_name(query)

        try:
            y = int(year) if year is not None else None
        except (TypeError, ValueError):
            y = None

        def _exact_name(s: Any) -> bool:
            return (
                _normalize_metron_display_name(getattr(s, "display_name", None))
                == query_norm
            )

        def _in_window(s: Any) -> bool:
            # A null win-year or a candidate missing ``year_began`` leaves the
            # window unevaluable -> treat as not-satisfied (fall back to the
            # exact-name acceptor), never as a pass.
            if y is None:
                return False
            began = getattr(s, "year_began", None)
            if began is None:
                return False
            end = getattr(s, "year_end", None)
            return began <= y and (end is None or y <= end)

        if len(series_list) == 1:
            # BUI-494: no longer an unconditional trust — accept the sole
            # candidate iff it exact-name-matches OR is in-window (a lenient OR
            # floor; the multi-candidate branch below is stricter — exact-name
            # AND a unique in-window survivor).
            sole = series_list[0]
            return sole if (_exact_name(sole) or _in_window(sole)) else None

        exact_matches = [s for s in series_list if _exact_name(s)]
        if y is None:
            return None

        matches = [s for s in exact_matches if _in_window(s)]
        return matches[0] if len(matches) == 1 else None

    @staticmethod
    def _cache_year_key(year: Any) -> Optional[int]:
        """Normalize ``year`` the same way ``_disambiguate_series`` does.

        Used to build the ``_series_cache`` key so "1988" and 1988 collapse
        to the SAME entry instead of silently missing a hit on what is, to
        ``_disambiguate_series``, an identical query.
        """
        try:
            return int(year) if year is not None else None
        except (TypeError, ValueError):
            return None

    @_retry_once_on_rate_limit
    def resolve_series(self, series_query: str, year: Any = None) -> Optional[Any]:
        """Resolve a series name to a Metron series handle (BUI-473).

        This is the per-series HALF of :meth:`lookup_issue`'s two HTTP
        requests, split out so a batch resolving N issues from the SAME
        series can call this ONCE and reuse the returned handle across N
        :meth:`issue_in_series` calls, instead of paying a fresh
        ``series_list`` request per issue. :meth:`lookup_issue` itself is
        reimplemented in terms of this method plus :meth:`issue_in_series`,
        so its own behavior is unchanged — this just makes the reusable half
        independently callable.

        Checks (and, on a miss, populates) this instance's ``_series_cache``
        keyed on ``(masthead-mapped series_query, coerced year)`` — the SAME
        inputs ``_disambiguate_series`` bases its own decision on, so a cache
        hit can never reuse one query's resolution for a differently-spelled
        or differently-dated one. Only a GENUINE outcome is cached — a
        resolved series, or a clean "no match"/"ambiguous" from
        ``_disambiguate_series``. A transient failure (rate limit exhausted,
        connection error, 5xx exhausted) is deliberately NOT cached: caching
        it would let one throttled call permanently condemn every later issue
        of the same series for the rest of the batch, even after
        ``cmd_collection_record_win``'s own BUI-465 cooldown reopens the
        breaker — so a fresh attempt is always allowed to compete for a real
        answer after any error.

        Applies the BUI-487 masthead translation (``_map_annual_masthead``),
        searches Metron's ``series_list``, and disambiguates via
        :meth:`_disambiguate_series` (BUI-32/BUI-474/BUI-485) — this method's
        body IS that half of the original ``lookup_issue``, unchanged.

        Returns the mokkari series object Metron resolved, or ``None`` on no
        match, an ambiguous result, rate limit, network error, or missing
        credentials. MetronCredentialError is re-raised so callers can
        disable Metron for the rest of a batch rather than retrying on every
        win.
        """
        # Computed first, before anything that can raise, so it's always
        # bound for the blanket-exception log below (BUI-487) — mirrors the
        # original lookup_issue's own ordering.
        metron_query = _map_annual_masthead(series_query)
        cache_key = (metron_query, self._cache_year_key(year))
        if cache_key in self._series_cache:
            return self._series_cache[cache_key]

        try:
            session = self._get_session()
            series_list = session.series_list({"name": metron_query})
            if not series_list:
                self._series_cache[cache_key] = None
                return None

            best_series = self._disambiguate_series(series_list, year, metron_query)
            if best_series is None:
                logger.debug(
                    "Metron series ambiguous for %r (mapped from %r, year=%r): %d candidates",
                    metron_query, series_query, year, len(series_list),
                )
            self._series_cache[cache_key] = best_series
            return best_series
        except MetronCredentialError:
            raise
        except RateLimitError:
            raise  # handled by @_retry_once_on_rate_limit, not the blanket handler below
        except Exception as exc:  # noqa: BLE001  # Metron API failure — log and return None to skip enrichment
            # BUI-342: a genuine 5xx propagates to @_retry_once_on_rate_limit
            # for one capped retry + a ``degraded`` trip; a data-shape/404
            # ApiError does NOT (it stays a silent None, same as before).
            # NOT cached (see docstring) — a transient miss must not poison
            # every later issue of this series.
            if _is_server_error(exc):
                raise
            if _is_connection_error(exc):
                self.degraded = True
            logger.debug(
                "Metron series resolution failed for %r (mapped from %r): %s",
                metron_query, series_query, exc,
            )
            return None

    @_retry_once_on_rate_limit
    def issue_in_series(self, series: Any, issue_number: str | int) -> Optional[dict[str, Any]]:
        """Look up one issue within an already-resolved Metron series (BUI-473).

        The per-issue HALF of :meth:`lookup_issue`'s two HTTP requests,
        paired with :meth:`resolve_series`: given a series handle that method
        already returned (fresh or cached), spends the ``issues_list``
        request and returns the SAME dict shape :meth:`lookup_issue` returns
        — metron_id, cover_date, store_date, series_year_began,
        series_year_end, series_name, series_id. Deliberately uncached — an
        issue number within a series is genuinely per-issue, and a
        multi-issue run of one series has all-distinct issue numbers, so a
        cache here would get zero hits (BUI-465's own finding, reaffirmed by
        BUI-473's ticket).

        Returns ``None`` on no match, rate limit, network error, or missing
        credentials. MetronCredentialError is re-raised, same contract as
        :meth:`lookup_issue`.
        """
        try:
            session = self._get_session()
            issues = session.issues_list(
                {"series_id": series.id, "number": str(issue_number)}
            )
            if not issues:
                return None

            issue = issues[0]
            cover = issue.cover_date.isoformat() if issue.cover_date else None
            store = issue.store_date.isoformat() if issue.store_date else None
            return {
                "metron_id": issue.id,
                "cover_date": cover,
                "store_date": store,
                "series_year_began": series.year_began,
                "series_year_end": series.year_end,
                "series_name": series.display_name,
                "series_id": series.id,
            }
        except MetronCredentialError:
            raise
        except RateLimitError:
            raise  # handled by @_retry_once_on_rate_limit, not the blanket handler below
        except Exception as exc:  # noqa: BLE001  # Metron API failure — log and return None to skip enrichment
            # BUI-342: a genuine 5xx propagates to @_retry_once_on_rate_limit
            # for one capped retry + a ``degraded`` trip; a data-shape/404
            # ApiError does NOT (it stays a silent None, same as before).
            if _is_server_error(exc):
                raise
            if _is_connection_error(exc):
                self.degraded = True
            logger.debug(
                "Metron issue lookup failed for series id %s #%s: %s",
                getattr(series, "id", None), issue_number, exc,
            )
            return None

    def lookup_issue(
        self, series_query: str, issue_number: str | int, year: Any = None
    ) -> Optional[dict[str, Any]]:
        """Look up an issue by series name and number via the Metron API.

        Metron's series search is a substring match, so a common masthead
        (e.g. "Batman") can return hundreds of candidates. When there is more
        than one, they are first narrowed to an EXACT ``display_name`` match
        against ``series_query`` (BUI-485), then ``year`` (the issue's
        publication year from identify_data) disambiguates the survivors by
        publication range (BUI-32). If either step can't narrow to one, the
        lookup returns None so the row is flagged for manual series resolution
        rather than guessed.

        Before either of those steps, ``series_query`` is translated through
        ``_ANNUAL_MASTHEAD_TO_METRON`` (BUI-487) so an Annual/Giant-Size/
        Special filed under a masthead Metron doesn't use (e.g. "Uncanny
        X-Men Annual") is searched — and exact-matched — under the masthead
        Metron actually uses ("X-Men Annual"). A query with no table entry
        passes through unchanged.

        Returns a dict with metron_id, cover_date, store_date,
        series_year_began, series_year_end, series_name, series_id — or None
        on any failure (no match, ambiguous series, rate limit, network error,
        missing creds).

        MetronCredentialError is re-raised so callers can disable Metron for
        the rest of a batch rather than retrying on every win.

        BUI-473: reimplemented as :meth:`resolve_series` (the per-series
        half — map + ``series_list`` + disambiguate, cached across calls on
        this instance) followed by :meth:`issue_in_series` (the per-issue
        ``issues_list`` half). This method's own signature and return shape
        are UNCHANGED — it stays the convenience one-call entry point every
        existing caller already uses. A batch resolving N issues from the
        same series should call :meth:`resolve_series` once and
        :meth:`issue_in_series` per issue directly (or rely on
        :meth:`lookup_issue_request_cost` to pace itself honestly) to
        actually realize the savings, rather than switching away from this
        method.
        """
        best_series = self.resolve_series(series_query, year)
        if best_series is None:
            return None
        return self.issue_in_series(best_series, issue_number)

    def lookup_issue_request_cost(self, series_query: str, year: Any = None) -> int:
        """How many HTTP requests the NEXT ``lookup_issue(series_query, ...,
        year)`` call will actually spend (BUI-473).

        Three cases, mirroring exactly what ``lookup_issue`` (resolve_series
        then issue_in_series) will do:

        - Not yet resolved this batch -> ``REQUESTS_LOOKUP_ISSUE`` (both
          halves will run).
        - Already cached with a REAL series handle -> ``REQUESTS_ISSUE_IN_SERIES``
          (the series_list half is free; issue_in_series still runs).
        - Already cached as ``None`` (a genuine no-match/ambiguous result for
          this exact series+year) -> ``0``. ``lookup_issue`` returns ``None``
          the instant ``resolve_series`` does, WITHOUT ever calling
          ``issue_in_series`` — so charging ``REQUESTS_ISSUE_IN_SERIES`` here
          would over-count. This is exactly the shape of the manual-series
          backlog this ticket targets: repeated issues of a series Metron
          can't resolve, once that series' first miss is cached.

        A batch caller (``cmd_collection_record_win``'s ``_build_win_row``)
        calls this BEFORE ``lookup_issue`` and charges its per-minute pacing
        budget with the result, instead of the flat constant, so a run of N
        issues from one series paces itself on what Metron will actually be
        asked rather than what a single, uncached lookup would cost. Reading
        the cache here does not itself resolve anything (or mutate the
        cache) — it is a snapshot of what ``resolve_series`` would do right
        now, so it must be called BEFORE the paired ``lookup_issue``, not
        after (by which point the cache may already reflect this call).
        """
        metron_query = _map_annual_masthead(series_query)
        cache_key = (metron_query, self._cache_year_key(year))
        if cache_key not in self._series_cache:
            return REQUESTS_LOOKUP_ISSUE
        return REQUESTS_ISSUE_IN_SERIES if self._series_cache[cache_key] is not None else 0

    @_retry_once_on_rate_limit
    def lookup_issue_detail(self, metron_id: int) -> Optional[dict[str, Any]]:
        """Fetch full issue detail (variant cover names + creator credits) for a id.

        ``issues_list`` (used by :meth:`lookup_issue`) returns lightweight
        ``BaseIssue`` records with no variants and no credits, so variant
        resolution and creator-run resolution both need the detail endpoint
        ``session.issue(metron_id)`` (BUI-33, BUI-134).

        Returns ``{"variants": [name, ...], "credits": [{"creator": name,
        "creator_id": id, "roles": [role, ...]}, ...], "publisher": name}`` —
        the variant cover names, every creator credit, plus the publisher
        display name (e.g. ``"Marvel Comics"``; BUI-458) Metron has for the
        issue — or ``None`` on any failure. The full ``Issue`` this already
        fetches carries ``publisher``, so surfacing it costs no extra network
        call. ``publisher`` is ``None`` when Metron has no publisher on the
        issue (never a fabricated guess). MetronCredentialError is re-raised
        so a batch can disable Metron rather than retry per win.

        Note on ``creator_id``: a mokkari ``Credit`` exposes ``id`` (the credit
        row id, **not** the creator id) and ``creator`` (the creator's canonical
        name string). The creator id is therefore not available from the credit
        itself; callers that need to pin a creator id resolve it separately via
        :meth:`resolve_creator` and match on the canonical name. ``creator_id``
        is surfaced as ``None`` here for forward-compatibility.
        """
        try:
            session = self._get_session()
            issue = session.issue(metron_id)
            variants = [
                v.name for v in (getattr(issue, "variants", None) or [])
                if getattr(v, "name", None)
            ]
            credits = self._extract_credits(issue)
            publisher = self._extract_publisher(issue)
            return {"variants": variants, "credits": credits, "publisher": publisher}
        except MetronCredentialError:
            raise
        except RateLimitError:
            raise  # handled by @_retry_once_on_rate_limit, not the blanket handler below
        except Exception as exc:  # noqa: BLE001  # Metron API failure — log and return None to skip variant enrichment
            # BUI-342: a genuine 5xx propagates to @_retry_once_on_rate_limit
            # for one capped retry + a ``degraded`` trip; a data-shape/404
            # ApiError does NOT (it stays a silent None, same as before).
            if _is_server_error(exc):
                raise
            if _is_connection_error(exc):
                self.degraded = True
            logger.debug("Metron issue-detail lookup failed for id %s: %s", metron_id, exc)
            return None

    @_retry_once_on_rate_limit
    def lookup_issue_by_id(self, metron_id: int) -> Optional[dict[str, Any]]:
        """Fetch the book Metron actually has at a KNOWN metron_id (BUI-501).

        The reverse direction from :meth:`lookup_issue`/:meth:`issue_in_series`
        (series NAME + issue number -> id): given a ``metron_id`` already
        stamped on a collection row, this asks Metron what that id actually
        IS, so a caller can check whether the id agrees with the row's own
        evidence (its ``release_date``) — the wrong-metron_id-on-correct-date
        class BUI-500 uncovered, where a live, well-formed, but WRONG id can
        pass every LOCAL, date-only predicate (BUI-493) undetected.

        Deliberately separate from :meth:`lookup_issue_detail` (also an
        id -> data fetch via the same ``session.issue(metron_id)`` call, but
        scoped to variant/credit/publisher ENRICHMENT for an id already
        trusted) — this one is scoped to the identity fields a trust
        VERIFICATION predicate needs (cover date + series), not enrichment.

        Returns ``{"metron_id", "cover_date", "series_id", "series_name"}`` —
        ``cover_date`` is an ISO date string (or ``None`` if Metron has none),
        mirroring :meth:`issue_in_series`'s shape — or ``None`` on any
        failure (id not found, rate limit, network error, missing creds).
        ``MetronCredentialError`` re-raises so a batch caller can disable
        Metron for the rest of a run rather than retry per row.
        """
        try:
            session = self._get_session()
            issue = session.issue(metron_id)
            cover = issue.cover_date.isoformat() if getattr(issue, "cover_date", None) else None
            series = getattr(issue, "series", None)
            return {
                "metron_id": metron_id,
                "cover_date": cover,
                "series_id": getattr(series, "id", None),
                "series_name": getattr(series, "name", None),
            }
        except MetronCredentialError:
            raise
        except RateLimitError:
            raise  # handled by @_retry_once_on_rate_limit, not the blanket handler below
        except Exception as exc:  # noqa: BLE001  # Metron API failure — log and return None so the caller skips this row (BUI-501: fail-closed, no verdict from an uncertain call)
            # BUI-342: a genuine 5xx propagates to @_retry_once_on_rate_limit
            # for one capped retry + a ``degraded`` trip; a data-shape/404
            # ApiError does NOT (it stays a silent None, same as before).
            if _is_server_error(exc):
                raise
            if _is_connection_error(exc):
                self.degraded = True
            logger.debug("Metron issue-by-id lookup failed for id %s: %s", metron_id, exc)
            return None

    @staticmethod
    def _extract_credits(issue: Any) -> list[dict[str, Any]]:
        """Pull ``[{creator, creator_id, roles}]`` from a mokkari issue detail.

        Each mokkari ``Credit`` has ``creator`` (canonical name string) and
        ``role`` (a ``list`` of ``GenericItem``, each with ``id``/``name``).
        Roles are lowercased so callers can compare case-insensitively.
        """
        out: list[dict[str, Any]] = []
        for credit in (getattr(issue, "credits", None) or []):
            creator = getattr(credit, "creator", None)
            if not creator:
                continue
            roles = [
                r.name.strip().lower()
                for r in (getattr(credit, "role", None) or [])
                if getattr(r, "name", None)
            ]
            out.append({
                "creator": creator,
                "creator_id": None,
                "roles": roles,
            })
        return out

    @staticmethod
    def _extract_publisher(issue: Any) -> Optional[str]:
        """Pull the publisher display name (e.g. ``"Marvel Comics"``) from a
        mokkari issue detail (BUI-458).

        A mokkari ``Issue`` carries ``publisher`` as a ``GenericItem`` whose
        ``name`` is the display name. Returns ``None`` when the attribute is
        absent, is not a ``GenericItem`` with a string ``name``, or is blank —
        so a Metron miss degrades to a null publisher (the pre-upload audit's
        backstop) rather than a fabricated or defaulted value. The ``isinstance``
        guard also keeps a bare ``MagicMock`` issue from injecting a mock as a
        publisher name in tests.
        """
        publisher = getattr(issue, "publisher", None)
        name = getattr(publisher, "name", None)
        if isinstance(name, str) and name.strip():
            return name.strip()
        return None

    @_retry_once_on_rate_limit
    def resolve_creator(self, name: str) -> Optional[dict[str, Any]]:
        """Resolve a creator name to a Metron creator ``{id, name}`` (BUI-134).

        Pins the creator's Metron id so "John Romita Jr." and "John Romita Sr."
        can never be conflated by a loose name match. Returns the canonical
        record on an unambiguous match, else ``None``:

        - exactly one candidate                         -> use it
        - multiple, exactly one whose name equals the
          query case-insensitively                      -> that one
        - otherwise (zero, or still ambiguous)          -> ``None``

        The canonical ``name`` is what later filters the per-issue credits
        (whose ``creator`` field is a name string, not an id). MetronCredentialError
        is re-raised so callers can disable Metron for the rest of a batch.
        """
        try:
            session = self._get_session()
            candidates = session.creators_list({"name": name})
            if not candidates:
                return None
            if len(candidates) == 1:
                c = candidates[0]
                return {"id": c.id, "name": c.name}
            query_norm = name.strip().lower()
            exact = [c for c in candidates if (c.name or "").strip().lower() == query_norm]
            if len(exact) == 1:
                c = exact[0]
                return {"id": c.id, "name": c.name}
            logger.debug(
                "Metron creator ambiguous for %r: %d candidates",
                name, len(candidates),
            )
            return None
        except MetronCredentialError:
            raise
        except RateLimitError:
            raise  # handled by @_retry_once_on_rate_limit, not the blanket handler below
        except Exception as exc:  # noqa: BLE001  # Metron API failure — log and return None
            # BUI-342: a genuine 5xx propagates to @_retry_once_on_rate_limit
            # for one capped retry + a ``degraded`` trip; a data-shape/404
            # ApiError does NOT (it stays a silent None, same as before).
            if _is_server_error(exc):
                raise
            if _is_connection_error(exc):
                self.degraded = True
            logger.debug("Metron creator lookup failed for %r: %s", name, exc)
            return None

    @_retry_once_on_rate_limit
    def resolve_creator_run(
        self,
        series_id: int,
        creator_id: int,
        creator_name: str,
        role: str = "penciller",
    ) -> Optional[dict[str, Any]]:
        """Resolve the EXACT set of issues a creator holds ``role`` on (BUI-134).

        Grounds creator-run membership in Metron's per-issue credits rather than
        model memory, which silently drops DISCONTINUOUS runs (e.g. John Romita
        Jr.'s Uncanny X-Men #175–211 AND his ~1993 #287/#300–311 second stint).

        Strategy:
          1. ``issues_list({series_id, creator: creator_id})`` returns the candidate
             set — every issue where this creator (pinned by **id**, so JR vs Sr
             never collide) has *any* credit. This is what catches both stints.
          2. For each candidate, fetch the issue detail and confirm the creator
             actually holds ``role`` (default ``"penciller"``). The issue-list
             ``creator`` filter is role-agnostic, so an issue where JR only wrote
             (never pencilled) is dropped here.
          3. An issue whose detail carries **no credits at all** can't be
             confirmed or refuted — Metron's credit data is thin on older
             Silver/Bronze books — so it is reported as a low-confidence warning
             rather than silently treated as "not in the run".

        ``role`` matching is EXPLICIT: an issue is in the run iff the creator has
        a credit whose role set contains ``role`` (case-insensitive). The default
        ``"penciller"`` does NOT auto-include ``layouts``/``breakdowns``/etc.;
        pass those role names explicitly to widen it.

        Returns ``{"issues": [{number, metron_id, cover_date}], "warnings":
        [{number, metron_id, reason}]}`` sorted by numeric issue number, or
        ``None`` on a hard API failure. ``issues`` is the confirmed run;
        ``warnings`` flags no-credit issues for the caller to surface.
        """
        role_norm = (role or "").strip().lower()
        try:
            session = self._get_session()
            candidates = session.issues_list(
                {"series_id": series_id, "creator": creator_id}
            )
        except MetronCredentialError:
            raise
        except RateLimitError:
            raise  # handled by @_retry_once_on_rate_limit, not the blanket handler below
        except Exception as exc:  # noqa: BLE001
            # BUI-342: a genuine 5xx propagates to @_retry_once_on_rate_limit
            # for one capped retry + a ``degraded`` trip; a data-shape/404
            # ApiError does NOT (it stays a silent None, same as before).
            if _is_server_error(exc):
                raise
            if _is_connection_error(exc):
                self.degraded = True
            logger.debug(
                "Metron creator-run candidate lookup failed (series=%s creator=%s): %s",
                series_id, creator_id, exc,
            )
            return None

        creator_norm = (creator_name or "").strip().lower()
        in_run: list[dict[str, Any]] = []
        warnings: list[dict[str, Any]] = []

        for idx, cand in enumerate(candidates):
            if self.degraded:
                # BUI-344: the breaker already tripped (5xx / rate-limit-exhausted
                # / connection error) on an earlier candidate's lookup_issue_detail
                # call. Mirror the batch-breaker pattern cmd_collection_record_win
                # uses (_check_metron_degraded, commands.py) — stop iterating so
                # the rest of the candidate list doesn't each pay its own capped
                # retry sleep against a down Metron. Surface every un-attempted
                # candidate as a warning so the run reads as clearly incomplete
                # rather than silently truncated.
                for skipped in candidates[idx:]:
                    warnings.append({
                        "number": getattr(skipped, "number", None),
                        "metron_id": getattr(skipped, "id", None),
                        "reason": (
                            "skipped: Metron breaker tripped on an earlier "
                            "candidate (5xx/rate-limit/connection error) — "
                            "run is incomplete (BUI-344)"
                        ),
                    })
                break
            metron_id = getattr(cand, "id", None)
            number = getattr(cand, "number", None)
            if metron_id is None:
                continue
            detail = self.lookup_issue_detail(metron_id)
            cover = getattr(cand, "cover_date", None)
            cover_iso = cover.isoformat() if cover else None
            if detail is None:
                warnings.append({
                    "number": number,
                    "metron_id": metron_id,
                    "reason": "issue detail fetch failed",
                })
                continue
            credits = detail.get("credits") or []
            if not credits:
                warnings.append({
                    "number": number,
                    "metron_id": metron_id,
                    "reason": "no credits in Metron (older book?) — run membership unverified",
                })
                continue
            # Collect all credit entries whose creator name matches (case-insensitive).
            # mokkari Credit.creator is a name string, NOT a creator id — the id on the
            # credit row is the credit-row id, not the Metron creator id (BUI-198).
            # If two distinct Metron creators share the same canonical name and both
            # credit this issue (e.g. the resolved creator as Writer and a namesake as
            # Penciller), they will appear as two separate credit entries with the same
            # creator string.  Detecting two entries for the same name is therefore the
            # only in-band signal that a same-name collision may be present; when found,
            # the issue is demoted to a warning rather than silently added to the run.
            matching_credits = [
                c for c in credits
                if (c.get("creator") or "").strip().lower() == creator_norm
            ]
            if len(matching_credits) > 1:
                # Multiple credit entries share this creator name → possible same-name
                # collision between distinct Metron creators (BUI-198).  Can't confirm
                # run membership without a creator-id on the credit; surface as warning.
                warnings.append({
                    "number": number,
                    "metron_id": metron_id,
                    "reason": (
                        f"same-name collision guard: {len(matching_credits)} credit entries "
                        f"share the name {creator_name!r} — run membership unverified "
                        "(mokkari Credit carries no creator id; BUI-198)"
                    ),
                })
                continue
            if not matching_credits:
                # The issue is in the id-constrained candidate set (the resolved creator
                # has *some* credit here), yet no credit's name string matches the resolved
                # canonical name.  This is name drift — punctuation/comma variants like
                # "John Romita, Jr." vs "John Romita Jr." — not a real absence.  Exclude it
                # from the run (we can't confirm the role) but WARN so a truncated run is
                # visible rather than a silent drop (BUI-198).
                warnings.append({
                    "number": number,
                    "metron_id": metron_id,
                    "reason": (
                        f"no credit name matched {creator_name!r} despite the id-pinned "
                        "candidate filter — likely name drift (punctuation variant); "
                        "run membership unverified (BUI-198)"
                    ),
                })
                continue
            holds_role = role_norm in (matching_credits[0].get("roles") or [])
            if holds_role:
                in_run.append({
                    "number": number,
                    "metron_id": metron_id,
                    "cover_date": cover_iso,
                })

        def _num_key(entry: dict[str, Any]) -> tuple[float, str]:
            raw = str(entry.get("number") or "")
            try:
                return (float(raw), raw)
            except ValueError:
                return (float("inf"), raw)

        in_run.sort(key=_num_key)
        warnings.sort(key=_num_key)
        return {"issues": in_run, "warnings": warnings}

    def format_series_name(self, series_data: dict[str, Any]) -> str:
        """Format a canonical series name per R62.

        "{name} ({year_began} - {end_year})" where end_year is the numeric
        year_end when present, else "Present".
        """
        name = series_data.get("series_name", "")
        year_began = series_data.get("series_year_began", "")
        year_end = series_data.get("series_year_end")
        end_str = str(year_end) if year_end else "Present"
        return f"{name} ({year_began} - {end_str})"
