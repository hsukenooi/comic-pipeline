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
        except Exception as exc:  # noqa: BLE001  # Metron API failure — log and return None to skip enrichment
            logger.debug("Metron lookup failed for %r #%s: %s", series_query, issue_number, exc)
            return None

    def lookup_issue_detail(self, metron_id: int) -> Optional[dict[str, Any]]:
        """Fetch full issue detail (variant cover names + creator credits) for a id.

        ``issues_list`` (used by :meth:`lookup_issue`) returns lightweight
        ``BaseIssue`` records with no variants and no credits, so variant
        resolution and creator-run resolution both need the detail endpoint
        ``session.issue(metron_id)`` (BUI-33, BUI-134).

        Returns ``{"variants": [name, ...], "credits": [{"creator": name,
        "creator_id": id, "roles": [role, ...]}, ...]}`` — the variant cover
        names plus every creator credit Metron has for the issue — or ``None``
        on any failure. MetronCredentialError is re-raised so a batch can
        disable Metron rather than retry per win.

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
            return {"variants": variants, "credits": credits}
        except MetronCredentialError:
            raise
        except Exception as exc:  # noqa: BLE001  # Metron API failure — log and return None to skip variant enrichment
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
        except Exception as exc:  # noqa: BLE001  # Metron API failure — log and return None
            logger.debug("Metron creator lookup failed for %r: %s", name, exc)
            return None

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
          1. ``issues_list({series, creator: creator_id})`` returns the candidate
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
                {"series": series_id, "creator": creator_id}
            )
        except MetronCredentialError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "Metron creator-run candidate lookup failed (series=%s creator=%s): %s",
                series_id, creator_id, exc,
            )
            return None

        creator_norm = (creator_name or "").strip().lower()
        in_run: list[dict[str, Any]] = []
        warnings: list[dict[str, Any]] = []

        for cand in candidates:
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
