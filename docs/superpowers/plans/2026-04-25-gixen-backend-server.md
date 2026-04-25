# Gixen Backend Server Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a FastAPI server on Mac Mini that proxies all Gixen operations and persists comic FMV + bid history in SQLite, accessible from two laptops over Tailscale.

**Architecture:** FastAPI app with asyncio background sync loop runs on Mac Mini as a LaunchAgent. Both laptops use gixen-cli in thin-client mode (`GIXEN_SERVER_URL` env var), routing all writes through the server. The server is the single Gixen writer; SQLite stores the history. All `GixenClient` (blocking `requests`-based) calls inside FastAPI are wrapped in `asyncio.to_thread()`. Background sync and API request handlers use separate `GixenClient` instances.

**Tech Stack:** Python 3.11+, FastAPI, Uvicorn, SQLite (WAL mode), httpx (test client), pytest, Click (existing), requests (existing)

---

## File Map

| File | Action | Purpose |
|---|---|---|
| `requirements.txt` | Modify | Add fastapi, uvicorn[standard], httpx |
| `server/__init__.py` | Create | Package marker |
| `server/db.py` | Create | SQLite schema, init, all DB queries |
| `server/main.py` | Create | FastAPI app, all endpoints, background sync |
| `server/install.sh` | Create | LaunchAgent installer for Mac Mini |
| `tests/test_server_db.py` | Create | Unit tests for db.py |
| `tests/test_server_api.py` | Create | HTTP endpoint tests using TestClient |
| `cli.py` | Modify | Thin-client mode, new `--comic`/`--fmv-*` flags on `add` |
| `~/Projects/Brain v3.0/.claude/commands/comic/snipe-add.md` | Modify | Pass FMV flags to `gixen add` |

---

### Task 1: Add dependencies and create server package

**Files:**
- Modify: `requirements.txt`
- Create: `server/__init__.py`

- [ ] **Step 1: Update requirements.txt**

Replace the entire file with:
```
requests
click
python-dotenv
pytest
fastapi
"uvicorn[standard]"
httpx
```

- [ ] **Step 2: Create server package**

```bash
mkdir -p server
touch server/__init__.py
```

- [ ] **Step 3: Install dependencies**

```bash
cd ~/conductor/workspaces/gixen-cli/shanghai
pip install -r requirements.txt
```

Expected: installs fastapi, uvicorn, httpx without errors.

- [ ] **Step 4: Commit**

```bash
git add requirements.txt server/__init__.py
git commit -m "chore: add fastapi/uvicorn/httpx dependencies"
```

---

### Task 2: SQLite layer (server/db.py)

**Files:**
- Create: `server/db.py`
- Create: `tests/test_server_db.py`

- [ ] **Step 1: Write failing tests**

Create `tests/test_server_db.py`:
```python
"""Unit tests for server/db.py — all use tmp_path, no disk side effects."""
import sqlite3
import pytest
from pathlib import Path

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from server.db import init_db, upsert_comic, insert_bid, get_bid_by_item_id, \
    update_bid, update_bid_status, delete_bid, get_all_bids


@pytest.fixture
def db(tmp_path):
    conn = init_db(tmp_path / "test.db")
    yield conn
    conn.close()


def test_init_creates_tables(db):
    cur = db.execute("SELECT name FROM sqlite_master WHERE type='table'")
    tables = {row[0] for row in cur}
    assert "comics" in tables
    assert "bids" in tables


def test_wal_mode_enabled(db):
    row = db.execute("PRAGMA journal_mode").fetchone()
    assert row[0] == "wal"


def test_upsert_comic_inserts(db):
    comic_id = upsert_comic(db, title="Amazing Spider-Man", issue="300",
                            year=1988, grade=9.2,
                            fmv_low=800.0, fmv_high=1000.0,
                            fmv_comps=12, fmv_confidence="high",
                            fmv_notes="Key issue")
    assert isinstance(comic_id, int)
    row = db.execute("SELECT * FROM comics WHERE id=?", (comic_id,)).fetchone()
    assert row["title"] == "Amazing Spider-Man"
    assert row["grade"] == 9.2
    assert row["fmv_confidence"] == "high"


def test_upsert_comic_updates_on_conflict(db):
    id1 = upsert_comic(db, title="X-Men", issue="1", year=1963, grade=8.0,
                       fmv_low=500.0, fmv_high=700.0,
                       fmv_comps=5, fmv_confidence="medium", fmv_notes="")
    id2 = upsert_comic(db, title="X-Men", issue="1", year=1963, grade=8.0,
                       fmv_low=550.0, fmv_high=750.0,
                       fmv_comps=8, fmv_confidence="high", fmv_notes="Updated")
    assert id1 == id2
    row = db.execute("SELECT fmv_low FROM comics WHERE id=?", (id1,)).fetchone()
    assert row["fmv_low"] == 550.0


def test_insert_bid(db):
    bid_id = insert_bid(db, item_id="123456789", max_bid=800.0,
                        comic_id=None, bid_offset=6, snipe_group=0,
                        seller="seller1")
    assert isinstance(bid_id, int)
    row = db.execute("SELECT * FROM bids WHERE id=?", (bid_id,)).fetchone()
    assert row["item_id"] == "123456789"
    assert row["status"] == "PENDING"
    assert row["max_bid"] == 800.0


def test_insert_bid_links_comic(db):
    comic_id = upsert_comic(db, "Hulk", "181", 1974, 9.0,
                            50.0, 70.0, 10, "high", "")
    bid_id = insert_bid(db, "987654321", 60.0, comic_id, 6, 0, "seller2")
    row = db.execute("SELECT comic_id FROM bids WHERE id=?", (bid_id,)).fetchone()
    assert row["comic_id"] == comic_id


def test_get_bid_by_item_id(db):
    insert_bid(db, "111222333", 50.0, None, 6, 0, "s")
    row = get_bid_by_item_id(db, "111222333")
    assert row is not None
    assert row["item_id"] == "111222333"


def test_get_bid_by_item_id_missing(db):
    assert get_bid_by_item_id(db, "999999999") is None


def test_update_bid(db):
    insert_bid(db, "444555666", 50.0, None, 6, 0, "s")
    update_bid(db, "444555666", max_bid=60.0, bid_offset=10, snipe_group=1)
    row = get_bid_by_item_id(db, "444555666")
    assert row["max_bid"] == 60.0
    assert row["snipe_group"] == 1


def test_update_bid_status(db):
    insert_bid(db, "777888999", 100.0, None, 6, 0, "s")
    update_bid_status(db, "777888999", status="WON",
                      winning_bid=85.0, resolved_at="2026-04-25T12:00:00")
    row = get_bid_by_item_id(db, "777888999")
    assert row["status"] == "WON"
    assert row["winning_bid"] == 85.0
    assert row["resolved_at"] == "2026-04-25T12:00:00"


def test_delete_bid_marks_purged(db):
    insert_bid(db, "555444333", 30.0, None, 6, 0, "s")
    delete_bid(db, "555444333")
    row = get_bid_by_item_id(db, "555444333")
    assert row["status"] == "PURGED"


def test_get_all_bids_returns_list(db):
    insert_bid(db, "100000001", 10.0, None, 6, 0, "s")
    insert_bid(db, "100000002", 20.0, None, 6, 0, "s")
    rows = get_all_bids(db)
    item_ids = [r["item_id"] for r in rows]
    assert "100000001" in item_ids
    assert "100000002" in item_ids
```

- [ ] **Step 2: Run tests to confirm they fail**

```bash
pytest tests/test_server_db.py -v
```

Expected: `ImportError: cannot import name 'init_db' from 'server.db'`

- [ ] **Step 3: Implement server/db.py**

Create `server/db.py`:
```python
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

DB_PATH = Path.home() / ".gixen-server" / "db.sqlite"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS comics (
    id              INTEGER PRIMARY KEY,
    title           TEXT NOT NULL,
    issue           TEXT NOT NULL,
    year            INTEGER NOT NULL,
    grade           REAL,
    fmv_low         REAL,
    fmv_high        REAL,
    fmv_comps       INTEGER,
    fmv_confidence  TEXT CHECK(fmv_confidence IN ('high', 'medium', 'low') OR fmv_confidence IS NULL),
    fmv_notes       TEXT,
    fmv_updated_at  TEXT,
    created_at      TEXT DEFAULT (datetime('now')),
    UNIQUE(title, issue, year, grade)
);

CREATE TABLE IF NOT EXISTS bids (
    id              INTEGER PRIMARY KEY,
    item_id         TEXT NOT NULL,
    comic_id        INTEGER REFERENCES comics(id),
    max_bid         REAL NOT NULL,
    bid_offset      INTEGER DEFAULT 6,
    snipe_group     INTEGER DEFAULT 0,
    status          TEXT DEFAULT 'PENDING',
    winning_bid     REAL,
    seller          TEXT,
    auction_end_at  TEXT,
    notes           TEXT,
    added_at        TEXT DEFAULT (datetime('now')),
    resolved_at     TEXT
);
"""


def init_db(path: Path = DB_PATH) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    conn.commit()
    return conn


def upsert_comic(
    conn: sqlite3.Connection,
    title: str,
    issue: str,
    year: int,
    grade: Optional[float],
    fmv_low: Optional[float],
    fmv_high: Optional[float],
    fmv_comps: Optional[int],
    fmv_confidence: Optional[str],
    fmv_notes: Optional[str],
) -> int:
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """
        INSERT INTO comics (title, issue, year, grade, fmv_low, fmv_high,
                            fmv_comps, fmv_confidence, fmv_notes, fmv_updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(title, issue, year, grade) DO UPDATE SET
            fmv_low         = excluded.fmv_low,
            fmv_high        = excluded.fmv_high,
            fmv_comps       = excluded.fmv_comps,
            fmv_confidence  = excluded.fmv_confidence,
            fmv_notes       = excluded.fmv_notes,
            fmv_updated_at  = excluded.fmv_updated_at
        """,
        (title, issue, year, grade, fmv_low, fmv_high,
         fmv_comps, fmv_confidence, fmv_notes, now),
    )
    conn.commit()
    row = conn.execute(
        "SELECT id FROM comics WHERE title=? AND issue=? AND year=? AND grade IS ?",
        (title, issue, year, grade),
    ).fetchone()
    return row["id"]


def insert_bid(
    conn: sqlite3.Connection,
    item_id: str,
    max_bid: float,
    comic_id: Optional[int],
    bid_offset: int,
    snipe_group: int,
    seller: Optional[str],
) -> int:
    cur = conn.execute(
        """
        INSERT INTO bids (item_id, max_bid, comic_id, bid_offset, snipe_group, seller)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (item_id, max_bid, comic_id, bid_offset, snipe_group, seller),
    )
    conn.commit()
    return cur.lastrowid


def get_bid_by_item_id(conn: sqlite3.Connection, item_id: str) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM bids WHERE item_id=? ORDER BY id DESC LIMIT 1",
        (item_id,),
    ).fetchone()


def update_bid(
    conn: sqlite3.Connection,
    item_id: str,
    max_bid: float,
    bid_offset: int,
    snipe_group: int,
) -> None:
    conn.execute(
        "UPDATE bids SET max_bid=?, bid_offset=?, snipe_group=? WHERE item_id=? AND status='PENDING'",
        (max_bid, bid_offset, snipe_group, item_id),
    )
    conn.commit()


def update_bid_status(
    conn: sqlite3.Connection,
    item_id: str,
    status: str,
    winning_bid: Optional[float] = None,
    resolved_at: Optional[str] = None,
) -> None:
    conn.execute(
        "UPDATE bids SET status=?, winning_bid=?, resolved_at=? WHERE item_id=? AND status NOT IN ('PURGED')",
        (status, winning_bid, resolved_at, item_id),
    )
    conn.commit()


def delete_bid(conn: sqlite3.Connection, item_id: str) -> None:
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "UPDATE bids SET status='PURGED', resolved_at=? WHERE item_id=? AND status='PENDING'",
        (now, item_id),
    )
    conn.commit()


def get_all_bids(conn: sqlite3.Connection) -> list:
    return conn.execute("SELECT * FROM bids ORDER BY added_at DESC").fetchall()


def get_pending_bids(conn: sqlite3.Connection) -> list:
    return conn.execute("SELECT * FROM bids WHERE status='PENDING'").fetchall()


def mark_bids_purged(conn: sqlite3.Connection, item_ids: list[str]) -> None:
    if not item_ids:
        return
    now = datetime.now(timezone.utc).isoformat()
    placeholders = ",".join("?" * len(item_ids))
    conn.execute(
        f"UPDATE bids SET status='PURGED', resolved_at=? WHERE item_id IN ({placeholders})",
        [now, *item_ids],
    )
    conn.commit()
```

- [ ] **Step 4: Run tests to confirm they pass**

```bash
pytest tests/test_server_db.py -v
```

Expected: all 13 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add server/db.py tests/test_server_db.py
git commit -m "feat: SQLite schema and DB query layer"
```

---

### Task 3: FastAPI skeleton + POST /api/comics

**Files:**
- Create: `server/main.py`
- Create: `tests/test_server_api.py`

- [ ] **Step 1: Write failing tests**

Create `tests/test_server_api.py`:
```python
"""HTTP endpoint tests — GixenClient is mocked, DB uses tmp_path."""
import pytest
from unittest.mock import MagicMock, patch
from fastapi.testclient import TestClient

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def _make_mock_gixen():
    m = MagicMock()
    m.list_snipes.return_value = []
    m.add_snipe.return_value = None
    m.modify_snipe.return_value = None
    m.remove_snipe.return_value = True
    m.purge_completed.return_value = None
    return m


@pytest.fixture
def api(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("GIXEN_USERNAME", "testuser")
    monkeypatch.setenv("GIXEN_PASSWORD", "testpass")
    monkeypatch.setenv("GIXEN_SYNC_ENABLED", "false")
    mock = _make_mock_gixen()
    with patch("server.main.GixenClient", return_value=mock):
        from server.main import app
        with TestClient(app) as client:
            client.mock_gixen = mock
            yield client


def test_health(api):
    r = api.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_upsert_comic(api):
    r = api.post("/api/comics", json={
        "title": "Amazing Spider-Man",
        "issue": "300",
        "year": 1988,
        "grade": 9.2,
        "fmv_low": 800.0,
        "fmv_high": 1000.0,
        "fmv_comps": 12,
        "fmv_confidence": "high",
        "fmv_notes": "Key issue",
    })
    assert r.status_code == 200
    data = r.json()
    assert data["id"] > 0
    assert data["title"] == "Amazing Spider-Man"


def test_upsert_comic_twice_updates(api):
    payload = {"title": "X-Men", "issue": "1", "year": 1963,
               "grade": 8.0, "fmv_low": 500.0, "fmv_high": 700.0,
               "fmv_comps": 5, "fmv_confidence": "medium", "fmv_notes": ""}
    r1 = api.post("/api/comics", json=payload)
    payload["fmv_low"] = 550.0
    r2 = api.post("/api/comics", json=payload)
    assert r1.json()["id"] == r2.json()["id"]
    assert r2.json()["fmv_low"] == 550.0


def test_upsert_comic_missing_required_field(api):
    r = api.post("/api/comics", json={"title": "X-Men", "issue": "1"})  # missing year
    assert r.status_code == 422
```

- [ ] **Step 2: Run tests to confirm they fail**

```bash
pytest tests/test_server_api.py::test_health tests/test_server_api.py::test_upsert_comic -v
```

Expected: `ModuleNotFoundError: No module named 'server.main'`

- [ ] **Step 3: Implement server/main.py skeleton + POST /api/comics**

Create `server/main.py`:
```python
"""Gixen backend server — FastAPI app with SQLite storage and Gixen proxy."""
import asyncio
import os
import re
import sqlite3
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, field_validator

from gixen_client import GixenClient
from server.db import (
    DB_PATH, init_db, upsert_comic, insert_bid, get_bid_by_item_id,
    update_bid, update_bid_status, delete_bid, get_all_bids,
    get_pending_bids, mark_bids_purged,
)

# ---------------------------------------------------------------------------
# App state (module-level globals, injected via env vars for testing)
# ---------------------------------------------------------------------------

_db: Optional[sqlite3.Connection] = None
_api_client: Optional[GixenClient] = None
_sync_client: Optional[GixenClient] = None
_api_lock = asyncio.Lock()


def _get_db() -> sqlite3.Connection:
    assert _db is not None, "DB not initialized"
    return _db


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    global _db, _api_client, _sync_client, _api_lock
    db_path = Path(os.getenv("DB_PATH", str(DB_PATH)))
    _db = init_db(db_path)
    _api_client = GixenClient()
    _sync_client = GixenClient()
    _api_lock = asyncio.Lock()

    sync_task = None
    if os.getenv("GIXEN_SYNC_ENABLED", "true") == "true":
        sync_task = asyncio.create_task(_sync_loop())

    yield

    if sync_task:
        sync_task.cancel()
    _db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    _db.close()


app = FastAPI(lifespan=lifespan)

# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------

class UpsertComicRequest(BaseModel):
    title: str
    issue: str
    year: int
    grade: Optional[float] = None
    fmv_low: Optional[float] = None
    fmv_high: Optional[float] = None
    fmv_comps: Optional[int] = None
    fmv_confidence: Optional[str] = None
    fmv_notes: Optional[str] = None

    @field_validator("fmv_confidence")
    @classmethod
    def validate_confidence(cls, v):
        if v is not None and v not in ("high", "medium", "low"):
            raise ValueError("fmv_confidence must be high, medium, or low")
        return v


class AddBidRequest(BaseModel):
    item_id: str
    max_bid: float
    bid_offset: int = 6
    snipe_group: int = 0
    # Optional comic context
    comic: Optional[str] = None
    issue: Optional[str] = None
    year: Optional[int] = None
    grade: Optional[float] = None
    fmv_low: Optional[float] = None
    fmv_high: Optional[float] = None
    fmv_comps: Optional[int] = None
    fmv_confidence: Optional[str] = None
    fmv_notes: Optional[str] = None

    @field_validator("item_id")
    @classmethod
    def item_id_numeric(cls, v):
        if not re.match(r"^\d+$", v):
            raise ValueError("item_id must be numeric")
        return v

    @field_validator("max_bid")
    @classmethod
    def max_bid_positive(cls, v):
        if v <= 0:
            raise ValueError("max_bid must be positive")
        return v


class EditBidRequest(BaseModel):
    max_bid: float
    bid_offset: int = 6
    snipe_group: int = 0

    @field_validator("max_bid")
    @classmethod
    def max_bid_positive(cls, v):
        if v <= 0:
            raise ValueError("max_bid must be positive")
        return v


class PurgeRequest(BaseModel):
    sibling_ids: list[str] = []

# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/api/comics")
async def api_upsert_comic(req: UpsertComicRequest):
    db = _get_db()
    comic_id = upsert_comic(
        db,
        title=req.title,
        issue=req.issue,
        year=req.year,
        grade=req.grade,
        fmv_low=req.fmv_low,
        fmv_high=req.fmv_high,
        fmv_comps=req.fmv_comps,
        fmv_confidence=req.fmv_confidence,
        fmv_notes=req.fmv_notes,
    )
    row = db.execute("SELECT * FROM comics WHERE id=?", (comic_id,)).fetchone()
    return dict(row)
```

- [ ] **Step 4: Run tests**

```bash
pytest tests/test_server_api.py::test_health tests/test_server_api.py::test_upsert_comic tests/test_server_api.py::test_upsert_comic_twice_updates tests/test_server_api.py::test_upsert_comic_missing_required_field -v
```

Expected: all 4 PASS.

- [ ] **Step 5: Commit**

```bash
git add server/main.py tests/test_server_api.py
git commit -m "feat: FastAPI skeleton with POST /api/comics"
```

---

### Task 4: POST /api/bids

**Files:**
- Modify: `server/main.py`
- Modify: `tests/test_server_api.py`

- [ ] **Step 1: Add failing tests** — append to `tests/test_server_api.py`:

```python
def test_add_bid_no_comic(api):
    r = api.post("/api/bids", json={
        "item_id": "123456789",
        "max_bid": 50.0,
    })
    assert r.status_code == 200
    data = r.json()
    assert data["item_id"] == "123456789"
    assert data["status"] == "PENDING"
    api.mock_gixen.add_snipe.assert_called_once()


def test_add_bid_with_comic_links_fmv(api):
    r = api.post("/api/bids", json={
        "item_id": "987654321",
        "max_bid": 800.0,
        "comic": "Amazing Spider-Man",
        "issue": "300",
        "year": 1988,
        "grade": 9.2,
        "fmv_low": 800.0,
        "fmv_high": 1000.0,
        "fmv_comps": 12,
        "fmv_confidence": "high",
        "fmv_notes": "",
    })
    assert r.status_code == 200
    data = r.json()
    assert data["comic_id"] is not None


def test_add_bid_invalid_item_id(api):
    r = api.post("/api/bids", json={"item_id": "abc", "max_bid": 50.0})
    assert r.status_code == 422


def test_add_bid_negative_max_bid(api):
    r = api.post("/api/bids", json={"item_id": "123456789", "max_bid": -10.0})
    assert r.status_code == 422


def test_add_bid_gixen_error_returns_503(api):
    from gixen_client import GixenError
    api.mock_gixen.add_snipe.side_effect = GixenError("Gixen down")
    r = api.post("/api/bids", json={"item_id": "111222333", "max_bid": 50.0})
    assert r.status_code == 503
```

- [ ] **Step 2: Run to confirm they fail**

```bash
pytest tests/test_server_api.py::test_add_bid_no_comic -v
```

Expected: `404 Not Found` (endpoint doesn't exist yet)

- [ ] **Step 3: Add POST /api/bids to server/main.py** — append after `api_upsert_comic`:

```python
from decimal import Decimal
from gixen_client import GixenError


@app.post("/api/bids")
async def api_add_bid(req: AddBidRequest):
    db = _get_db()

    # Upsert comic if FMV context provided
    comic_id = None
    if req.comic and req.issue and req.year is not None:
        comic_id = upsert_comic(
            db,
            title=req.comic,
            issue=req.issue,
            year=req.year,
            grade=req.grade,
            fmv_low=req.fmv_low,
            fmv_high=req.fmv_high,
            fmv_comps=req.fmv_comps,
            fmv_confidence=req.fmv_confidence,
            fmv_notes=req.fmv_notes,
        )

    # Call Gixen (blocking I/O → thread)
    try:
        async with _api_lock:
            await asyncio.to_thread(
                _api_client.add_snipe,
                req.item_id,
                Decimal(str(req.max_bid)),
                bid_offset=req.bid_offset,
                snipe_group=req.snipe_group,
            )
    except GixenError as e:
        raise HTTPException(status_code=503, detail=str(e))

    # Record in DB (seller not available until next list_snipes sync)
    bid_id = insert_bid(
        db,
        item_id=req.item_id,
        max_bid=req.max_bid,
        comic_id=comic_id,
        bid_offset=req.bid_offset,
        snipe_group=req.snipe_group,
        seller=None,
    )
    row = db.execute("SELECT * FROM bids WHERE id=?", (bid_id,)).fetchone()
    return dict(row)
```

- [ ] **Step 4: Run tests**

```bash
pytest tests/test_server_api.py::test_add_bid_no_comic tests/test_server_api.py::test_add_bid_with_comic_links_fmv tests/test_server_api.py::test_add_bid_invalid_item_id tests/test_server_api.py::test_add_bid_negative_max_bid tests/test_server_api.py::test_add_bid_gixen_error_returns_503 -v
```

Expected: all 5 PASS.

- [ ] **Step 5: Commit**

```bash
git add server/main.py tests/test_server_api.py
git commit -m "feat: POST /api/bids proxies to Gixen and persists bid"
```

---

### Task 5: GET /api/snipes

**Files:**
- Modify: `server/main.py`
- Modify: `tests/test_server_api.py`

- [ ] **Step 1: Add failing tests** — append to `tests/test_server_api.py`:

```python
def test_get_snipes_empty(api):
    api.mock_gixen.list_snipes.return_value = []
    r = api.get("/api/snipes")
    assert r.status_code == 200
    assert r.json() == []


def test_get_snipes_merges_fmv(api):
    # Add a bid with comic context first
    api.post("/api/bids", json={
        "item_id": "555666777",
        "max_bid": 60.0,
        "comic": "Hulk",
        "issue": "181",
        "year": 1974,
        "grade": 9.0,
        "fmv_low": 50.0,
        "fmv_high": 70.0,
        "fmv_comps": 8,
        "fmv_confidence": "high",
        "fmv_notes": "",
    })
    # Mock Gixen returning the same item
    api.mock_gixen.list_snipes.return_value = [{
        "item_id": "555666777",
        "title": "Incredible Hulk #181",
        "max_bid": "60.00 USD",
        "current_bid": "45.00 USD",
        "status": "SCHEDULED",
        "time_to_end": "5h 0m",
        "seller": "comicseller",
        "snipe_group": "0",
        "bid_offset": "6",
        "bid_offset_mirror": "6",
        "dbidid": "abc123",
    }]
    r = api.get("/api/snipes")
    assert r.status_code == 200
    snipes = r.json()
    assert len(snipes) == 1
    assert snipes[0]["item_id"] == "555666777"
    assert snipes[0]["fmv_low"] == 50.0
    assert snipes[0]["fmv_confidence"] == "high"


def test_get_snipes_gixen_error_returns_503(api):
    from gixen_client import GixenError
    api.mock_gixen.list_snipes.side_effect = GixenError("Gixen down")
    r = api.get("/api/snipes")
    assert r.status_code == 503
```

- [ ] **Step 2: Run to confirm they fail**

```bash
pytest tests/test_server_api.py::test_get_snipes_empty -v
```

Expected: `404 Not Found`

- [ ] **Step 3: Add GET /api/snipes to server/main.py** — append after `api_add_bid`:

```python
@app.get("/api/snipes")
async def api_get_snipes():
    db = _get_db()
    try:
        async with _api_lock:
            gixen_snipes = await asyncio.to_thread(_api_client.list_snipes)
    except GixenError as e:
        raise HTTPException(status_code=503, detail=str(e))

    # Build a lookup of DB bids by item_id → joined with comic FMV
    db_rows = db.execute("""
        SELECT b.*, c.title AS comic_title, c.issue AS comic_issue,
               c.year AS comic_year, c.grade AS comic_grade,
               c.fmv_low, c.fmv_high, c.fmv_comps,
               c.fmv_confidence, c.fmv_notes
        FROM bids b
        LEFT JOIN comics c ON b.comic_id = c.id
        WHERE b.status = 'PENDING'
    """).fetchall()
    db_by_item = {row["item_id"]: dict(row) for row in db_rows}

    result = []
    for snipe in gixen_snipes:
        merged = dict(snipe)
        db_data = db_by_item.get(snipe["item_id"], {})
        for key in ("fmv_low", "fmv_high", "fmv_comps", "fmv_confidence",
                    "fmv_notes", "comic_title", "comic_issue",
                    "comic_year", "comic_grade", "comic_id"):
            merged[key] = db_data.get(key)
        result.append(merged)

    return result
```

- [ ] **Step 4: Run tests**

```bash
pytest tests/test_server_api.py::test_get_snipes_empty tests/test_server_api.py::test_get_snipes_merges_fmv tests/test_server_api.py::test_get_snipes_gixen_error_returns_503 -v
```

Expected: all 3 PASS.

- [ ] **Step 5: Commit**

```bash
git add server/main.py tests/test_server_api.py
git commit -m "feat: GET /api/snipes with FMV join"
```

---

### Task 6: PATCH, DELETE, and POST /api/purge

**Files:**
- Modify: `server/main.py`
- Modify: `tests/test_server_api.py`

- [ ] **Step 1: Add failing tests** — append to `tests/test_server_api.py`:

```python
def test_edit_bid(api):
    api.post("/api/bids", json={"item_id": "200000001", "max_bid": 50.0})
    r = api.patch("/api/bids/200000001", json={"max_bid": 75.0, "bid_offset": 10, "snipe_group": 0})
    assert r.status_code == 200
    assert r.json()["max_bid"] == 75.0
    api.mock_gixen.modify_snipe.assert_called_once()


def test_edit_bid_not_found(api):
    from gixen_client import GixenSnipeNotFoundError
    api.mock_gixen.modify_snipe.side_effect = GixenSnipeNotFoundError("not found")
    r = api.patch("/api/bids/999999999", json={"max_bid": 75.0, "bid_offset": 6, "snipe_group": 0})
    assert r.status_code == 404


def test_remove_bid(api):
    api.post("/api/bids", json={"item_id": "300000001", "max_bid": 50.0})
    r = api.delete("/api/bids/300000001")
    assert r.status_code == 200
    api.mock_gixen.remove_snipe.assert_called_once()


def test_purge(api):
    r = api.post("/api/purge", json={"sibling_ids": []})
    assert r.status_code == 200
    data = r.json()
    assert "purged_completed" in data
    assert "removed_siblings" in data
    api.mock_gixen.list_snipes.assert_called()
    api.mock_gixen.purge_completed.assert_called_once()
```

- [ ] **Step 2: Run to confirm they fail**

```bash
pytest tests/test_server_api.py::test_edit_bid tests/test_server_api.py::test_remove_bid tests/test_server_api.py::test_purge -v
```

Expected: `404 Not Found`

- [ ] **Step 3: Add PATCH, DELETE, POST /api/purge to server/main.py** — append after `api_get_snipes`:

```python
from gixen_client import GixenSnipeNotFoundError


@app.patch("/api/bids/{item_id}")
async def api_edit_bid(item_id: str, req: EditBidRequest):
    if not re.match(r"^\d+$", item_id):
        raise HTTPException(status_code=422, detail="item_id must be numeric")
    db = _get_db()
    try:
        async with _api_lock:
            await asyncio.to_thread(
                _api_client.modify_snipe,
                item_id,
                Decimal(str(req.max_bid)),
                bid_offset=req.bid_offset,
                snipe_group=req.snipe_group,
            )
    except GixenSnipeNotFoundError:
        raise HTTPException(status_code=404, detail=f"Item {item_id} not in Gixen")
    except GixenError as e:
        raise HTTPException(status_code=503, detail=str(e))

    update_bid(db, item_id, req.max_bid, req.bid_offset, req.snipe_group)
    row = get_bid_by_item_id(db, item_id)
    if row is None:
        # Bid wasn't in DB (added before server existed) — return minimal info
        return {"item_id": item_id, "max_bid": req.max_bid, "status": "PENDING"}
    return dict(row)


@app.delete("/api/bids/{item_id}")
async def api_remove_bid(item_id: str):
    if not re.match(r"^\d+$", item_id):
        raise HTTPException(status_code=422, detail="item_id must be numeric")
    db = _get_db()
    try:
        async with _api_lock:
            await asyncio.to_thread(_api_client.remove_snipe, item_id)
    except GixenSnipeNotFoundError:
        raise HTTPException(status_code=404, detail=f"Item {item_id} not in Gixen")
    except GixenError as e:
        raise HTTPException(status_code=503, detail=str(e))

    delete_bid(db, item_id)
    return {"item_id": item_id, "status": "PURGED"}


@app.post("/api/purge")
async def api_purge(req: PurgeRequest):
    db = _get_db()

    # 1. Sync first to capture any outstanding WON/LOST transitions
    await _sync_gixen(db, _sync_client)

    # 2. Collect completed bid item_ids before purging Gixen
    completed = db.execute(
        "SELECT item_id FROM bids WHERE status IN ('WON','LOST','ENDED','FAILED')"
    ).fetchall()
    completed_ids = [r["item_id"] for r in completed]

    # 3. Purge completed on Gixen
    try:
        async with _api_lock:
            await asyncio.to_thread(_api_client.purge_completed)
    except GixenError as e:
        raise HTTPException(status_code=503, detail=str(e))

    # 4. Mark completed bids as PURGED in DB
    mark_bids_purged(db, completed_ids)

    # 5. Remove sibling snipes
    removed = 0
    for sibling_id in req.sibling_ids:
        try:
            async with _api_lock:
                await asyncio.to_thread(_api_client.remove_snipe, sibling_id)
            delete_bid(db, sibling_id)
            removed += 1
        except GixenError:
            pass  # best-effort sibling removal

    return {"purged_completed": len(completed_ids), "removed_siblings": removed}
```

- [ ] **Step 4: Run tests**

```bash
pytest tests/test_server_api.py::test_edit_bid tests/test_server_api.py::test_edit_bid_not_found tests/test_server_api.py::test_remove_bid tests/test_server_api.py::test_purge -v
```

Expected: all 4 PASS.

- [ ] **Step 5: Run full test suite**

```bash
pytest tests/test_server_db.py tests/test_server_api.py -v
```

Expected: all tests PASS.

- [ ] **Step 6: Commit**

```bash
git add server/main.py tests/test_server_api.py
git commit -m "feat: PATCH/DELETE /api/bids and POST /api/purge with sync-first"
```

---

### Task 7: Background sync loop

**Files:**
- Modify: `server/main.py`
- Modify: `tests/test_server_api.py`

- [ ] **Step 1: Add failing test** — append to `tests/test_server_api.py`:

```python
def test_sync_captures_won_status(api):
    """Sync updates bid status when Gixen reports WON."""
    # Add a bid so there's a DB record
    api.post("/api/bids", json={"item_id": "400000001", "max_bid": 50.0})

    # Mock Gixen returning the item as WON
    api.mock_gixen.list_snipes.return_value = [{
        "item_id": "400000001",
        "title": "Test",
        "max_bid": "50.00 USD",
        "current_bid": "42.00 USD",
        "status": "WON",
        "time_to_end": "ENDED",
        "seller": "s",
        "snipe_group": "0",
        "bid_offset": "6",
        "bid_offset_mirror": "6",
        "dbidid": "xyz",
    }]

    # Trigger sync via purge (which calls _sync_gixen internally)
    api.post("/api/purge", json={"sibling_ids": []})

    # Now check DB — status should be WON
    r = api.get("/api/snipes")
    # Item is WON so no longer in "PENDING" — but we can query DB directly
    # via a second purge to see what was captured
    r2 = api.post("/api/purge", json={"sibling_ids": []})
    assert r2.json()["purged_completed"] >= 1
```

- [ ] **Step 2: Add `_sync_gixen` function and `_sync_loop` to server/main.py** — add before the lifespan context manager (it's referenced by `api_purge` already):

Insert this block right after the `_get_db()` function definition:

```python
_GIXEN_TO_DB_STATUS = {
    "SCHEDULED": "PENDING",
    "WON": "WON",
    "LOST": "LOST",
    "FAILED": "FAILED",
    "ENDED": "ENDED",
}

SYNC_INTERVAL = int(os.getenv("GIXEN_SYNC_INTERVAL", "600"))  # seconds


async def _sync_gixen(db: sqlite3.Connection, client: GixenClient) -> None:
    """Pull current Gixen state and update DB bid statuses."""
    try:
        snipes = await asyncio.to_thread(client.list_snipes)
    except GixenError:
        return  # sync is best-effort; don't crash if Gixen is down

    now = datetime.now(timezone.utc).isoformat()
    gixen_item_ids = {s["item_id"] for s in snipes}

    for snipe in snipes:
        gixen_status = snipe.get("status", "")
        db_status = _GIXEN_TO_DB_STATUS.get(gixen_status)
        if db_status and db_status not in ("PENDING",):
            # Capture terminal state
            current_bid = snipe.get("current_bid", "")
            winning_bid = None
            if current_bid:
                try:
                    winning_bid = float(current_bid.split()[0])
                except (ValueError, IndexError):
                    pass
            update_bid_status(db, snipe["item_id"], db_status, winning_bid, now)

        # Update seller while we have fresh data
        if snipe.get("seller"):
            db.execute(
                "UPDATE bids SET seller=? WHERE item_id=? AND status='PENDING'",
                (snipe["seller"], snipe["item_id"]),
            )

    db.commit()

    # Mark bids PURGED if they've disappeared from Gixen but are still PENDING in DB
    pending_bids = get_pending_bids(db)
    vanished = [b["item_id"] for b in pending_bids if b["item_id"] not in gixen_item_ids]
    mark_bids_purged(db, vanished)


async def _sync_loop() -> None:
    await asyncio.sleep(SYNC_INTERVAL)  # initial delay before first sync
    while True:
        db = _get_db()
        await _sync_gixen(db, _sync_client)
        await asyncio.sleep(SYNC_INTERVAL)
```

- [ ] **Step 3: Run tests**

```bash
pytest tests/test_server_api.py::test_sync_captures_won_status -v
pytest tests/test_server_db.py tests/test_server_api.py -v
```

Expected: all PASS.

- [ ] **Step 4: Commit**

```bash
git add server/main.py tests/test_server_api.py
git commit -m "feat: background sync loop captures WON/LOST before purge"
```

---

### Task 8: CLI thin-client mode

**Files:**
- Modify: `cli.py`
- Modify: `tests/test_gixen_client.py` (add CLI thin-client tests)

The CLI gains a `_server_request` helper. When `GIXEN_SERVER_URL` is set, each command routes through the server instead of calling Gixen directly.

- [ ] **Step 1: Add failing CLI tests** — append to `tests/test_gixen_client.py`:

```python
# ---------------------------------------------------------------------------
# CLI thin-client mode tests
# ---------------------------------------------------------------------------

from cli import cli as cli_app
import responses as responses_lib  # noqa — we use requests_mock below


def test_cli_add_posts_to_server(monkeypatch):
    """When GIXEN_SERVER_URL is set, `add` POSTs to server."""
    import responses as resp_lib
    monkeypatch.setenv("GIXEN_SERVER_URL", "http://localhost:8080")
    monkeypatch.setenv("GIXEN_USERNAME", "u")
    monkeypatch.setenv("GIXEN_PASSWORD", "p")

    runner = CliRunner()
    with runner.isolated_filesystem():
        with patch("cli.requests") as mock_req:
            mock_resp = MagicMock()
            mock_resp.raise_for_status.return_value = None
            mock_resp.json.return_value = {
                "item_id": "123456789", "status": "PENDING", "max_bid": 50.0
            }
            mock_req.post.return_value = mock_resp
            mock_req.get.return_value = mock_resp

            result = runner.invoke(cli_app, ["add", "123456789", "50.00"])
            assert result.exit_code == 0
            assert "Added snipe" in result.output
            mock_req.post.assert_called_once()
            call_url = mock_req.post.call_args[0][0]
            assert "/api/bids" in call_url


def test_cli_add_with_fmv_flags(monkeypatch):
    """--comic/--grade/--fmv-* flags are sent to server."""
    monkeypatch.setenv("GIXEN_SERVER_URL", "http://localhost:8080")
    monkeypatch.setenv("GIXEN_USERNAME", "u")
    monkeypatch.setenv("GIXEN_PASSWORD", "p")

    runner = CliRunner()
    with runner.isolated_filesystem():
        with patch("cli.requests") as mock_req:
            mock_resp = MagicMock()
            mock_resp.raise_for_status.return_value = None
            mock_resp.json.return_value = {
                "item_id": "111222333", "status": "PENDING", "max_bid": 800.0,
                "comic_id": 1,
            }
            mock_req.post.return_value = mock_resp
            mock_req.get.return_value = mock_resp

            result = runner.invoke(cli_app, [
                "add", "111222333", "800.00",
                "--comic", "Amazing Spider-Man",
                "--issue", "300",
                "--year", "1988",
                "--grade", "9.2",
                "--fmv-low", "800",
                "--fmv-high", "1000",
            ])
            assert result.exit_code == 0
            payload = mock_req.post.call_args[1]["json"]
            assert payload["comic"] == "Amazing Spider-Man"
            assert payload["grade"] == 9.2
            assert payload["fmv_low"] == 800.0


def test_cli_server_unreachable_shows_error(monkeypatch):
    """When server is unreachable, add fails with clear message."""
    import requests as req_lib
    monkeypatch.setenv("GIXEN_SERVER_URL", "http://localhost:8080")
    monkeypatch.setenv("GIXEN_USERNAME", "u")
    monkeypatch.setenv("GIXEN_PASSWORD", "p")

    runner = CliRunner()
    with runner.isolated_filesystem():
        with patch("cli.requests") as mock_req:
            mock_req.post.side_effect = req_lib.ConnectionError("refused")
            result = runner.invoke(cli_app, ["add", "123456789", "50.00"])
            assert result.exit_code != 0
            assert "unreachable" in result.output.lower() or "error" in result.output.lower()
```

- [ ] **Step 2: Run to confirm they fail**

```bash
pytest tests/test_gixen_client.py::test_cli_add_posts_to_server -v
```

Expected: `AttributeError: module 'cli' has no attribute 'requests'` or similar

- [ ] **Step 3: Add thin-client mode to cli.py**

At the top of `cli.py`, after the existing imports, add:

```python
import requests
```

After the `_make_client()` function, add the server helper:

```python
def _server_url() -> str | None:
    return os.getenv("GIXEN_SERVER_URL", "").rstrip("/") or None


def _server_request(method: str, path: str, **kwargs):
    """Make a request to the gixen server. Raises SystemExit on failure."""
    url = f"{_server_url()}{path}"
    try:
        resp = getattr(requests, method)(url, **kwargs)
        resp.raise_for_status()
        return resp.json()
    except requests.ConnectionError:
        click.echo("Error: Server unreachable. Is the gixen server running?", err=True)
        sys.exit(1)
    except requests.HTTPError as e:
        detail = ""
        try:
            detail = e.response.json().get("detail", "")
        except Exception:
            pass
        click.echo(f"Error: Server returned {e.response.status_code}: {detail}", err=True)
        sys.exit(1)
```

Replace the `add` command with this extended version:

```python
@cli.command()
@click.argument("item_id")
@click.argument("max_bid")
@click.option("--offset", default=6, help="Seconds before end to place bid (1-15)")
@click.option("--group", default=0, help="Snipe group (0=none, 1-10)")
@click.option("--comic", default=None, help="Comic title (e.g. 'Amazing Spider-Man')")
@click.option("--issue", default=None, help="Issue number (e.g. '300')")
@click.option("--year", default=None, type=int, help="Publication year")
@click.option("--grade", default=None, type=float, help="CGC grade (e.g. 9.2)")
@click.option("--fmv-low", default=None, type=float, help="FMV range low end")
@click.option("--fmv-high", default=None, type=float, help="FMV range high end")
@click.option("--fmv-comps", default=None, type=int, help="Number of comps used")
@click.option("--fmv-confidence", default=None, help="FMV confidence: high/medium/low")
def add(item_id: str, max_bid: str, offset: int, group: int,
        comic: str | None, issue: str | None, year: int | None, grade: float | None,
        fmv_low: float | None, fmv_high: float | None,
        fmv_comps: int | None, fmv_confidence: str | None):
    """Add a snipe for an eBay item."""
    try:
        bid = Decimal(max_bid)
    except InvalidOperation:
        click.echo(f"Error: Invalid bid amount: {max_bid}", err=True)
        sys.exit(1)

    if server := _server_url():
        payload = {
            "item_id": item_id,
            "max_bid": float(bid),
            "bid_offset": offset,
            "snipe_group": group,
        }
        if comic:
            payload.update({
                "comic": comic, "issue": issue, "year": year,
                "grade": grade, "fmv_low": fmv_low, "fmv_high": fmv_high,
                "fmv_comps": fmv_comps, "fmv_confidence": fmv_confidence,
            })
        _server_request("post", "/api/bids", json=payload)
        _record_add(item_id)
        click.echo(f"Added snipe for {item_id} with max bid {bid}")
        return

    # Existing direct-Gixen path
    client = _make_client()
    try:
        existing = client.list_snipes()
        for s in existing:
            if s["item_id"] == item_id:
                existing_bid = s.get("max_bid", "?")
                click.echo(
                    f"Error: Snipe already exists for {item_id} "
                    f"with max bid {existing_bid}. "
                    f"Use `edit {item_id} {max_bid}` to change it.",
                    err=True,
                )
                sys.exit(1)
        client.add_snipe(item_id, bid, bid_offset=offset, snipe_group=group)
        _record_add(item_id)
        click.echo(f"Added snipe for {item_id} with max bid {bid}")
    except GixenError as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)
```

Replace the `list_snipes` command's core fetch section:

```python
@cli.command("list")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
@click.option(
    "--added-since",
    type=click.DateTime(formats=["%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"]),
    help="Only show snipes added via this CLI since the given time (ISO format)",
)
def list_snipes(as_json: bool, added_since: datetime | None):
    """Show all current snipes."""
    if server := _server_url():
        try:
            snipes = _server_request("get", "/api/snipes")
        except SystemExit:
            raise
    else:
        client = _make_client()
        try:
            snipes = client.list_snipes()
        except GixenError as e:
            click.echo(f"Error: {e}", err=True)
            sys.exit(1)

    # Filter by --added-since (rest of function unchanged from here)
    if added_since:
        history = _load_add_history()
        since_ts = added_since.replace(tzinfo=timezone.utc).timestamp()
        added_ids = {
            item_id
            for item_id, ts in history.items()
            if ts >= since_ts
        }
        snipes = [s for s in snipes if s["item_id"] in added_ids]

    if as_json:
        click.echo(json.dumps(snipes, indent=2))
        return

    if not snipes:
        click.echo("No snipes found.")
        return

    active = [s for s in snipes if s.get("time_to_end", "").upper() != "ENDED"]
    ended = [s for s in snipes if s.get("time_to_end", "").upper() == "ENDED"]

    if active:
        click.echo(click.style(f"Active Listings ({len(active)})", bold=True))
        click.echo(
            f"  {'Item':<17} {'Title':<40} {'Current':>10} {'Max Bid':>10} "
            f"{'Grp':>3} {'Time Left'}"
        )
        click.echo("  " + "-" * 99)
        for s in active:
            click.echo(
                f"  {s['item_id']:<17} "
                f"{s.get('title', '')[:38]:<40} "
                f"{_format_bid(s.get('current_bid', '')):>10} "
                f"{_format_bid(s.get('max_bid', '')):>10} "
                f"{_format_group(s.get('snipe_group', '')):>3} "
                f"{s.get('time_to_end', '')}"
            )
        click.echo()

    if ended:
        click.echo(click.style(f"Recently Ended ({len(ended)})", bold=True))
        click.echo(
            f"  {'Item':<17} {'Title':<40} {'Winning':>10} {'Max Bid':>10} "
            f"{'Grp':>3} {'Diff':>10}"
        )
        click.echo("  " + "-" * 99)
        for s in ended:
            winning = s.get("current_bid", "")
            max_bid = s.get("max_bid", "")
            diff = _calc_diff(max_bid, winning)
            click.echo(
                f"  {s['item_id']:<17} "
                f"{s.get('title', '')[:38]:<40} "
                f"{_format_bid(winning):>10} "
                f"{_format_bid(max_bid):>10} "
                f"{_format_group(s.get('snipe_group', '')):>3} "
                f"{diff:>10}"
            )
        click.echo()

    click.echo(f"{len(snipes)} snipe(s) total")
```

Replace the `edit` command:

```python
@cli.command()
@click.argument("item_id")
@click.argument("max_bid")
@click.option("--offset", default=6, help="Seconds before end to place bid (1-15)")
@click.option("--group", default=0, help="Snipe group (0=none, 1-10)")
def edit(item_id: str, max_bid: str, offset: int, group: int):
    """Change the bid on an existing snipe."""
    try:
        bid = Decimal(max_bid)
    except InvalidOperation:
        click.echo(f"Error: Invalid bid amount: {max_bid}", err=True)
        sys.exit(1)

    if _server_url():
        _server_request("patch", f"/api/bids/{item_id}",
                        json={"max_bid": float(bid), "bid_offset": offset, "snipe_group": group})
        click.echo(f"Updated snipe for {item_id} to max bid {bid}")
        return

    client = _make_client()
    try:
        client.modify_snipe(item_id, bid, bid_offset=offset, snipe_group=group)
        click.echo(f"Updated snipe for {item_id} to max bid {bid}")
    except GixenSnipeNotFoundError:
        click.echo(f"Error: Item {item_id} not found in your snipe list", err=True)
        sys.exit(1)
    except GixenError as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)
```

Replace the `remove` command:

```python
@cli.command()
@click.argument("item_id")
def remove(item_id: str):
    """Remove a snipe."""
    if _server_url():
        _server_request("delete", f"/api/bids/{item_id}")
        click.echo(f"Removed snipe for {item_id}")
        return

    client = _make_client()
    try:
        client.remove_snipe(item_id)
        click.echo(f"Removed snipe for {item_id}")
    except GixenSnipeNotFoundError:
        click.echo(f"Error: Item {item_id} not found in your snipe list", err=True)
        sys.exit(1)
    except GixenError as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)
```

Replace the `purge` command:

```python
@cli.command()
@click.option("--dry-run", is_flag=True, help="Show what would be purged without changes")
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation prompt")
def purge(dry_run: bool, yes: bool):
    """Remove completed snipes (and sibling snipes from groups with a win)."""
    if _server_url():
        # Get current snipes to find siblings
        snipes = _server_request("get", "/api/snipes")
        siblings = find_sibling_cleanup_targets(snipes)

        if siblings:
            click.echo(f"Will also remove {len(siblings)} sibling snipe(s):")
            for s in siblings:
                title = (s.get("title") or "")[:40]
                click.echo(f"  group {s.get('snipe_group', '?')}: {s['item_id']} \"{title}\"")

        if dry_run:
            click.echo("Dry run — no changes made.")
            return

        if siblings and not yes and not click.confirm("Continue?", default=False):
            click.echo("Aborted.")
            return

        result = _server_request("post", "/api/purge",
                                 json={"sibling_ids": [s["item_id"] for s in siblings]})
        click.echo(f"Purged {result['purged_completed']} completed snipe(s)")
        if result["removed_siblings"]:
            click.echo(f"Removed {result['removed_siblings']} sibling snipe(s)")
        return

    # Existing direct-Gixen path (unchanged)
    client = _make_client()
    try:
        snipes = client.list_snipes()
    except GixenError as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)

    siblings = find_sibling_cleanup_targets(snipes)

    if not siblings:
        if dry_run:
            click.echo("Would purge completed snipes")
            return
        try:
            client.purge_completed()
        except GixenError as e:
            click.echo(f"Error: {e}", err=True)
            sys.exit(1)
        click.echo("Purged completed snipes")
        return

    completed_count = sum(
        1 for s in snipes
        if s.get("status") in ("WON", "LOST", "FAILED", "ENDED")
    )

    click.echo(
        f"This will purge {completed_count} completed snipe(s) and remove "
        f"{len(siblings)} sibling snipe(s) from groups with a win:"
    )
    for s in siblings:
        title = (s.get("title") or "")[:40]
        click.echo(
            f"  group {s.get('snipe_group', '?')}: "
            f"{s['item_id']} \"{title}\" (was {s.get('status') or '?'})"
        )

    if dry_run:
        click.echo("Dry run — no changes made.")
        return

    if not yes and not click.confirm("Continue?", default=False):
        click.echo("Aborted.")
        return

    try:
        client.purge_completed()
    except GixenError as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)
    click.echo("Purged completed snipes")

    removed: list[dict] = []
    failures: list[tuple[str, str]] = []
    for s in siblings:
        try:
            client.remove_snipe(s["item_id"])
            removed.append(s)
        except GixenError as e:
            failures.append((s["item_id"], str(e)))
            click.echo(f"  failed to remove {s['item_id']}: {e}", err=True)

    if removed:
        click.echo(f"Removed {len(removed)} sibling snipe(s) from groups with a win.")

    if failures:
        sys.exit(1)
```

- [ ] **Step 4: Run CLI thin-client tests**

```bash
pytest tests/test_gixen_client.py::test_cli_add_posts_to_server tests/test_gixen_client.py::test_cli_add_with_fmv_flags tests/test_gixen_client.py::test_cli_server_unreachable_shows_error -v
```

Expected: all 3 PASS.

- [ ] **Step 5: Run full suite**

```bash
pytest tests/ -v
```

Expected: all tests PASS (existing tests still pass with no regressions).

- [ ] **Step 6: Commit**

```bash
git add cli.py tests/test_gixen_client.py
git commit -m "feat: CLI thin-client mode with GIXEN_SERVER_URL and FMV flags"
```

---

### Task 9: LaunchAgent installer (server/install.sh)

**Files:**
- Create: `server/install.sh`

This task has no unit tests — it's a shell script. Verify manually on Mac Mini.

- [ ] **Step 1: Create server/install.sh**

```bash
#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(dirname "$SCRIPT_DIR")"
VENV="$REPO_DIR/.venv"
SERVER_DIR="$HOME/.gixen-server"
PLIST="$HOME/Library/LaunchAgents/com.gixen.server.plist"

echo "==> Creating $SERVER_DIR"
mkdir -p "$SERVER_DIR"
chmod 700 "$SERVER_DIR"

if [ ! -f "$SERVER_DIR/.env" ]; then
  echo "==> Creating $SERVER_DIR/.env (fill in credentials)"
  cat > "$SERVER_DIR/.env" <<'ENV'
GIXEN_USERNAME=your_username_here
GIXEN_PASSWORD=your_password_here
DB_PATH=/Users/YOURUSERNAME/.gixen-server/db.sqlite
GIXEN_SYNC_ENABLED=true
GIXEN_SYNC_INTERVAL=600
ENV
  chmod 600 "$SERVER_DIR/.env"
  echo "    Edit $SERVER_DIR/.env before starting the server."
fi

echo "==> Creating Python venv"
python3 -m venv "$VENV"
"$VENV/bin/pip" install -q -r "$REPO_DIR/requirements.txt"

echo "==> Writing LaunchAgent plist to $PLIST"
cat > "$PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.gixen.server</string>
    <key>ProgramArguments</key>
    <array>
        <string>$VENV/bin/uvicorn</string>
        <string>server.main:app</string>
        <string>--host</string>
        <string>0.0.0.0</string>
        <string>--port</string>
        <string>8080</string>
    </array>
    <key>WorkingDirectory</key>
    <string>$REPO_DIR</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>ENV_FILE</key>
        <string>$SERVER_DIR/.env</string>
    </dict>
    <key>KeepAlive</key>
    <true/>
    <key>RunAtLoad</key>
    <true/>
    <key>StandardOutPath</key>
    <string>$SERVER_DIR/server.log</string>
    <key>StandardErrorPath</key>
    <string>$SERVER_DIR/server.error.log</string>
</dict>
</plist>
PLIST

echo "==> Loading LaunchAgent"
launchctl unload "$PLIST" 2>/dev/null || true
launchctl load -w "$PLIST"

echo ""
echo "Done. Server starting on port 8080."
echo "Logs: $SERVER_DIR/server.log"
echo "      $SERVER_DIR/server.error.log"
echo ""
echo "Test: curl http://localhost:8080/health"
```

Note: The server binds to `0.0.0.0:8080`. macOS firewall should be configured (System Settings → Firewall → Options) to block port 8080 from non-Tailscale interfaces, or use Little Snitch / PF rules. Binding to the Tailscale IP directly would fail at boot before Tailscale is ready.

- [ ] **Step 2: Make executable and commit**

```bash
chmod +x server/install.sh
git add server/install.sh
git commit -m "feat: LaunchAgent installer for Mac Mini deployment"
```

---

### Task 10: Update /comic:snipe-add in Brain v3.0

**Files:**
- Modify: `~/Projects/Brain v3.0/.claude/commands/comic/snipe-add.md`

- [ ] **Step 1: Update the "Add to Gixen" section**

In `~/Projects/Brain v3.0/.claude/commands/comic/snipe-add.md`, replace the "Add to Gixen" section:

```markdown
## Add to Gixen

**Run sequentially** — Gixen sessions are stateful and parallel adds will fail.

If `GIXEN_SERVER_URL` is set in the environment, pass FMV context flags:

```bash
cd ~/Projects/gixen-cli && .venv/bin/python cli.py add {item_id} {max_bid} \
  --comic "{title}" --issue "{issue}" --year {year} \
  --grade {grade_numeric} \
  --fmv-low {fmv_low} --fmv-high {fmv_high} \
  --fmv-comps {comps} --fmv-confidence {confidence}
```

If `GIXEN_SERVER_URL` is not set (direct Gixen mode):

```bash
cd ~/Projects/gixen-cli && .venv/bin/python cli.py add {item_id} {max_bid}
```

Use the grade numeric value only (e.g. `9.2`, not `"NM 9.2"`). If the grade is a letter-only raw grade, omit `--grade`.
```

- [ ] **Step 2: Verify the change**

```bash
cat ~/Projects/Brain\ v3.0/.claude/commands/comic/snipe-add.md | grep -A 10 "Add to Gixen"
```

Expected: shows the updated section with `--comic` flags.

- [ ] **Step 3: Commit in Brain v3.0 repo**

```bash
cd ~/Projects/Brain\ v3.0
git add .claude/commands/comic/snipe-add.md
git commit -m "feat: pass FMV flags to gixen add when GIXEN_SERVER_URL is set"
```

---

## Self-Review

### Spec coverage check

| Spec requirement | Task |
|---|---|
| SQLite schema (comics + bids tables) | Task 2 |
| WAL mode | Task 2 |
| `upsert_comic` on re-run | Task 2 |
| POST /api/bids writes to Gixen + DB | Task 4 |
| GET /api/snipes with FMV join | Task 5 |
| PATCH /api/bids/{item_id} | Task 6 |
| DELETE /api/bids/{item_id} | Task 6 |
| POST /api/purge with sync-first | Task 6, 7 |
| Background sync loop (asyncio, no APScheduler) | Task 7 |
| Separate GixenClient instances for sync vs API | Tasks 3, 7 |
| asyncio.to_thread() for all GixenClient calls | Tasks 4, 5, 6, 7 |
| Input validation (item_id numeric, max_bid positive) | Tasks 4, 6 |
| Parameterized queries | Task 2 |
| GIXEN_SERVER_URL thin-client mode | Task 8 |
| New `--comic`/`--fmv-*` flags on `gixen add` | Task 8 |
| Writes fail loudly when server unreachable | Task 8 |
| LaunchAgent with KeepAlive, stdout/stderr logging | Task 9 |
| chmod 700 ~/.gixen-server | Task 9 |
| /comic:snipe-add passes FMV flags | Task 10 |
| Gixen SCHEDULED → DB PENDING mapping | Task 7 |
| `grade` stored as real (numeric) | Task 2 |
| `auction_end_at` column exists but v1 leaves null | Task 2 |

No gaps found.

### Type consistency

- `upsert_comic` signature in db.py matches calls in main.py Tasks 3, 4
- `insert_bid` signature matches calls in Task 4
- `update_bid_status` called in Task 7 matches signature in Task 2
- `mark_bids_purged` called in Tasks 6, 7 matches Task 2
- `AddBidRequest.comic` field (string) maps to `upsert_comic(title=...)` — consistent
- `_server_request("post", "/api/bids", json=payload)` payload keys match `AddBidRequest` field names
