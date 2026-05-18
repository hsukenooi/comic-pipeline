"""gixen-overlay plugin stub.

PER-30 will fill in the actual implementation. This stub exists to:
- Prove the entry-point wiring works
- Provide the correct package/module structure for extraction
"""
from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING

from gixen.plugins import hookimpl

if TYPE_CHECKING:
    from fastapi import FastAPI


class GixenOverlayPlugin:
    @hookimpl
    def register_db_tables(self, conn: sqlite3.Connection) -> None:
        """Comic tables — implemented in PER-30."""

    @hookimpl
    def register_routes(self, app: "FastAPI") -> None:
        """Comic routes — implemented in PER-30."""

    @hookimpl
    def register_dashboard_tabs(self) -> list[dict]:
        """Comic dashboard tab — implemented in PER-30."""
        return []


plugin = GixenOverlayPlugin()
