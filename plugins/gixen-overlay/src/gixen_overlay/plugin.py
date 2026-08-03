"""gixen-overlay plugin — comic overlay for gixen-cli."""
from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING, Any

from gixen.plugins import hookimpl
from gixen_overlay.db import create_tables

if TYPE_CHECKING:
    from fastapi import FastAPI


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
        """BUI-617 (U3): stub so this hookspec lands green independently of
        the FMV-aware checks that fill it in (BUI-609 U4, a later wave).
        Returning `[]` contributes no advisories/checks — identical to no
        plugin being registered at all.
        """
        return []

    @hookimpl
    def on_bid_write_committed(
        self,
        conn: sqlite3.Connection,
        intent: Any,
        bid_row_id: int | None,
        check_results: list[dict],
    ) -> None:
        """BUI-617 (U3): stub — no-op. U4/U5 (later waves) fill this in to
        persist the FMV link the overlay's check_bid_write resolved, now
        that a bid row exists.
        """
        return None


plugin = GixenOverlayPlugin()
