"""gixen-overlay plugin — comic overlay for gixen-cli."""
from __future__ import annotations

import logging
import sqlite3
from typing import TYPE_CHECKING, Any

from gixen.plugins import hookimpl
from gixen_overlay.db import create_tables, link_fmv_to_bid
from gixen_overlay.models import LinkFmvRequest

if TYPE_CHECKING:
    from fastapi import FastAPI

logger = logging.getLogger(__name__)


class GixenOverlayPlugin:
    @hookimpl
    def register_db_tables(self, conn: sqlite3.Connection) -> None:
        create_tables(conn)

    @hookimpl
    def register_routes(self, app: "FastAPI") -> None:
        from gixen_overlay.routes import router
        app.include_router(router)

    @hookimpl
    def register_dashboard_tabs(self) -> list[dict]:
        return [{"label": "comics", "path": "/comics"}]

    @hookimpl
    def check_bid_write(self, conn: sqlite3.Connection, intent: Any) -> list[dict]:
        """BUI-620 (U4): the five FMV-aware pre-trade checks — over-FMV,
        recomputed cap, staleness, unpriced entry, duplicate comic. All
        read-only (v1 is advisory-only, KTD1); the implementations live in
        `gixen_overlay.policy` so this stays a thin dispatcher, mirroring
        how the host keeps its own checks in `server/policy.py`.

        Deferred import for the same reason `on_bid_write_committed` below
        defers its import of `gixen_overlay.routes`: `gixen_overlay.policy`
        itself imports `gixen_overlay.routes` (for `_resolve_fmv_for_link`),
        which imports `server.main` — still mid-import the first time this
        module loads at plugin-registration time. Safe here because this
        hook only runs at request time, long after `server.main` has
        finished importing.
        """
        from gixen_overlay.policy import check_bid_write as _check_bid_write
        return _check_bid_write(conn, intent)

    @hookimpl
    def on_bid_write_committed(
        self,
        conn: sqlite3.Connection,
        intent: Any,
        bid_row_id: int | None,
        check_results: list[dict],
    ) -> None:
        """BUI-619 (U5): persist the FMV link(s) named in the add payload's
        comic identity, now that a bid row exists.

        `intent.comic_identities` is `AddBidRequest.comic_identities`,
        threaded through by `api_add_bid` only — `[]` for an edit (PATCH
        never populates it) or an add with no identity, in which case this
        is a no-op. Each entry is `{comic_id|locg_id, grade}`; resolution
        reuses `/api/bids/{item_id}/link-fmv`'s own strategy
        (`_resolve_fmv_for_link`: comic_id direct lookup, then locg_id
        JOIN — no series/issue fallback, since the add payload never
        carries those). The first entry links `is_primary=True`; later
        entries (a future lot caller, KTD8) link `is_primary=False` —
        `link_fmv_to_bid`'s own "sole junction must be primary" fallback
        still promotes a later entry if an earlier one failed to resolve.

        The CLI's own post-add link-fmv call (kept for old-server
        compatibility — see `add_batch.py`'s `build_bid_payload`) still
        runs after this and lands on the same `link_fmv_to_bid` primary-
        replacement semantics (BUI-82: `ON CONFLICT ... DO UPDATE SET
        is_primary = 1`), so hook-link + legacy link converge to one
        junction row instead of duplicating.

        v1 is advisory-only (KTD1): a resolution or link failure for one
        identity is logged and skipped, never raised — the bid write
        already committed by the time this fires, so there is nothing left
        to roll back, and one bad identity must not cost the other entries
        in a lot payload their link. The host also wraps this whole hook
        call defensively (`notify_bid_write_committed`), but this stays
        cooperative rather than leaning on that alone.
        """
        if bid_row_id is None:
            return
        identities = intent.comic_identities or []
        if not identities:
            return

        # Deferred import: routes.py imports server.main, which is still
        # mid-import the first time this module loads at plugin-registration
        # time (same reason register_routes above imports gixen_overlay.
        # routes lazily) — safe here because this hook only runs at request
        # time, long after server.main has finished importing.
        from gixen_overlay.routes import _resolve_fmv_for_link

        for idx, identity in enumerate(identities):
            if not isinstance(identity, dict):
                logger.warning(
                    "on_bid_write_committed: comic identity entry is not a "
                    "dict (%r); skipping (bid_row_id=%s)", identity, bid_row_id,
                )
                continue
            grade = identity.get("grade")
            comic_id = identity.get("comic_id")
            locg_id = identity.get("locg_id")
            if grade is None or (comic_id is None and locg_id is None):
                logger.warning(
                    "on_bid_write_committed: comic identity %r missing grade "
                    "or comic_id/locg_id; skipping link (bid_row_id=%s)",
                    identity, bid_row_id,
                )
                continue
            try:
                req = LinkFmvRequest(grade=grade, comic_id=comic_id, locg_id=locg_id)
                fmv_row, attempted = _resolve_fmv_for_link(conn, req)
                if fmv_row is None:
                    logger.info(
                        "on_bid_write_committed: no FMV resolved for identity "
                        "%r (bid_row_id=%s, strategies attempted: %s) — "
                        "leaving unlinked; the CLI's post-add link-fmv call "
                        "or a later manual link can still land this",
                        identity, bid_row_id, attempted,
                    )
                    continue
                link_fmv_to_bid(
                    conn, bid_row_id, fmv_row["fmv_id"], is_primary=(idx == 0),
                )
            except Exception:
                logger.exception(
                    "on_bid_write_committed: failed to persist FMV link for "
                    "identity %r (bid_row_id=%s) — the bid write already "
                    "committed; only this link is lost",
                    identity, bid_row_id,
                )


plugin = GixenOverlayPlugin()
