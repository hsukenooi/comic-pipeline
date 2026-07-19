"""Thin mokkari wrapper for Metron API series + issue lookup."""
from __future__ import annotations

import functools
import logging
import os
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
    def _disambiguate_series(series_list: list[Any], year: Any) -> Optional[Any]:
        """Pick the series whose publication range includes ``year`` (BUI-32).

        - exactly one candidate            -> use it (trust the sole match)
        - multiple + ``year`` falls in exactly one ``[year_began, year_end]``
          range (year_end ``None`` means ongoing) -> that one
        - otherwise (no year, or still ambiguous) -> ``None`` so the caller
          falls back to ``needs_manual_series_canonical``
        """
        if len(series_list) == 1:
            return series_list[0]

        try:
            y = int(year) if year is not None else None
        except (TypeError, ValueError):
            y = None
        if y is None:
            return None

        matches = []
        for s in series_list:
            began = getattr(s, "year_began", None)
            if began is None:
                continue
            end = getattr(s, "year_end", None)
            if began <= y and (end is None or y <= end):
                matches.append(s)
        return matches[0] if len(matches) == 1 else None

    @_retry_once_on_rate_limit
    def lookup_issue(
        self, series_query: str, issue_number: str | int, year: Any = None
    ) -> Optional[dict[str, Any]]:
        """Look up an issue by series name and number via the Metron API.

        When the series name matches multiple series, ``year`` (the issue's
        publication year from identify_data) disambiguates by publication range
        (BUI-32). If it can't, the lookup returns None so the row is flagged for
        manual series resolution rather than guessed.

        Returns a dict with metron_id, cover_date, store_date,
        series_year_began, series_year_end, series_name, series_id — or None
        on any failure (no match, ambiguous series, rate limit, network error,
        missing creds).

        MetronCredentialError is re-raised so callers can disable Metron for
        the rest of a batch rather than retrying on every win.
        """
        try:
            session = self._get_session()
            series_list = session.series_list({"name": series_query})
            if not series_list:
                return None

            best_series = self._disambiguate_series(series_list, year)
            if best_series is None:
                logger.debug(
                    "Metron series ambiguous for %r (year=%r): %d candidates",
                    series_query, year, len(series_list),
                )
                return None

            issues = session.issues_list(
                {"series_id": best_series.id, "number": str(issue_number)}
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
                "series_year_began": best_series.year_began,
                "series_year_end": best_series.year_end,
                "series_name": best_series.display_name,
                "series_id": best_series.id,
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
            logger.debug("Metron lookup failed for %r #%s: %s", series_query, issue_number, exc)
            return None

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
