"""Thin mokkari wrapper for Metron API series + issue lookup."""
from __future__ import annotations

import logging
import os
from typing import Any, Optional

logger = logging.getLogger("locg")


class MetronCredentialError(RuntimeError):
    """Raised on first use when METRON_USERNAME or METRON_PASSWORD is absent."""


class MetronClient:
    """Lazy mokkari wrapper. The mokkari session is created on first use."""

    def __init__(self) -> None:
        self._session: Any = None

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
                {"series": best_series.id, "number": str(issue_number)}
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
        except Exception as exc:
            logger.debug("Metron lookup failed for %r #%s: %s", series_query, issue_number, exc)
            return None

    def lookup_issue_detail(self, metron_id: int) -> Optional[dict[str, Any]]:
        """Fetch full issue detail (incl. variant cover names) for a Metron id.

        ``issues_list`` (used by :meth:`lookup_issue`) returns lightweight
        ``BaseIssue`` records with no variants, so variant resolution needs the
        detail endpoint ``session.issue(metron_id)`` (BUI-33).

        Returns ``{"variants": [name, ...]}`` — the names of every variant cover
        Metron has for the issue — or ``None`` on any failure. MetronCredentialError
        is re-raised so a batch can disable Metron rather than retry per win.
        """
        try:
            session = self._get_session()
            issue = session.issue(metron_id)
            variants = [
                v.name for v in (getattr(issue, "variants", None) or [])
                if getattr(v, "name", None)
            ]
            return {"variants": variants}
        except MetronCredentialError:
            raise
        except Exception as exc:
            logger.debug("Metron issue-detail lookup failed for id %s: %s", metron_id, exc)
            return None

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
