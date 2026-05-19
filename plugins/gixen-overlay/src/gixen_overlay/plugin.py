"""gixen-overlay plugin — comic overlay for gixen-cli."""
from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING

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


plugin = GixenOverlayPlugin()
