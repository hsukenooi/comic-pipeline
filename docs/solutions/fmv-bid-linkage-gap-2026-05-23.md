# FMV / Condition Gap in Active Listings Dashboard

**Date:** 2026-05-23  
**Repos affected:** `comic-pipeline` (skills + gixen-overlay), `gixen-cli`  
**Symptom:** Active snipes on the `/comics` dashboard always show `—` for both **Cond** and **FMV** columns, regardless of whether `/comic:buy` ran FMV computation.

---

## What the dashboard needs

`GET /api/comics/snipes` (`routes.py`) produces `cond_grade` and `fmv_low`/`fmv_high` by aggregating over the `bid_fmvs` junction table:

```sql
SELECT b.*, MAX(CASE WHEN bf.is_primary = 1 THEN f.grade END) AS primary_grade,
       SUM(f.low) AS fmv_low_sum, SUM(f.high) AS fmv_high_sum,
       COUNT(bf.fmv_id) AS lot_count, ...
FROM bids b
LEFT JOIN bid_fmvs bf ON bf.bid_id = b.id
LEFT JOIN fmv f ON f.id = bf.fmv_id
WHERE b.status != 'PURGED'
GROUP BY b.id
```

`_build_comics_row()` in `routes.py` sets `needs_linking = True` and nulls out `fmv_low`/`fmv_high`/`cond_grade` whenever `lot_count == 0`. So the dashboard can only show data when a row exists in `bid_fmvs` linking the bid to an `fmv` row.

---

## How `bid_fmvs` is supposed to get populated

The `/comic:buy` workflow runs six steps. Steps 3 and 5 are the relevant ones:

**Step 3 — FMV** (`comic-fmv --batch`):  
Calls `POST /api/comics` → `upsert_comic()` + `upsert_fmv()` in `gixen_overlay/db.py`. Writes `comics` and `fmv` rows. The bid does not exist yet at this point, so nothing can be linked.

**Step 5 — Snipe Add** (`gixen-cli add {item_id} {max_bid}`):  
Calls `POST /api/bids` → `insert_bid()` in `server/db.py`. Creates a `bids` row with `fmv_id = NULL`. **`bid_fmvs` is never populated.** No call to `link_fmv_to_bid()` is made anywhere in this path.

---

## Root cause

Two bugs, one in each repo:

### Bug 1 — `gixen-cli` (`cli.py:225`, `server/main.py:749`)

`gixen-cli add` accepts only `item_id`, `max_bid`, `--offset`, `--group`. It has no mechanism to pass comic metadata to the server. `POST /api/bids` (`AddBidRequest`) also only accepts those four fields. Neither calls `link_fmv_to_bid()`.

### Bug 2 — Skills (`snipe-add.md`, `buy.md`)

`snipe-add.md` documents the `add` command with flags that do not exist:
```bash
gixen-cli add {item_id} {max_bid} \
  --comic "{title}" --issue "{issue}" --year {year} --grade {grade_numeric} \
  --locg-id {locg_id} [--locg-variant-id {locg_variant_id}]
```
These flags (`--comic`, `--issue`, `--year`, `--grade`, `--locg-id`, `--locg-variant-id`) are not defined in `cli.py`. Click returns an error on unknown options, so Claude falls back to running without them — the bid is added but never linked.

`buy.md` Step 3 also names the FMV command incorrectly:
- **Written:** `gixen-cli fmv --batch <working_list.json>`
- **Actual command:** `comic-fmv --batch <working_list.json>` (lives in `apps/fmv`, not `gixen-cli`)

---

## The fix (three issues)

### Issue A — New overlay endpoint: `POST /api/bids/{item_id}/link-fmv` (comic-pipeline)

Add to `plugins/gixen-overlay/src/gixen_overlay/routes.py`:

```python
class LinkFmvRequest(BaseModel):
    locg_id: int
    grade: float

@router.post("/api/bids/{item_id}/link-fmv")
async def api_link_fmv(item_id: str, req: LinkFmvRequest, request: Request):
    db = request.app.state.db
    bid = get_bid_by_item_id(db, item_id)
    if bid is None:
        raise HTTPException(404, f"Item {item_id} not in DB")
    row = db.execute(
        "SELECT f.id FROM fmv f JOIN comics c ON c.id = f.comic_id "
        "WHERE c.locg_id = ? AND f.grade = ?",
        (req.locg_id, req.grade),
    ).fetchone()
    if row is None:
        raise HTTPException(404, f"No FMV for locg_id={req.locg_id} grade={req.grade}")
    link_fmv_to_bid(db, bid["id"], row["id"], is_primary=True)
    return {"item_id": item_id, "fmv_id": row["id"], "linked": True}
```

Also add the `LinkFmvRequest` model to `models.py`.

### Issue B — Add `--locg-id` and `--grade` to `gixen-cli add` (gixen-cli)

In `cli.py add`:
```python
@click.option("--locg-id", type=int, default=None)
@click.option("--grade", type=float, default=None)
def add(item_id, max_bid, offset, group, locg_id, grade):
    ...
    if _server_url():
        payload = {...}  # existing
        _server_request("post", "/api/bids", json=payload)
        _record_add(item_id)
        # Link FMV if comic identity was provided
        if locg_id is not None and grade is not None:
            try:
                _server_request("post", f"/api/bids/{item_id}/link-fmv",
                                json={"locg_id": locg_id, "grade": grade})
            except SystemExit:
                click.echo("Warning: snipe added but FMV link failed", err=True)
        click.echo(f"Added snipe for {item_id} with max bid {bid}")
        return
```

The `try/except SystemExit` is needed because `_server_request` calls `sys.exit(1)` on HTTP errors — swallow it so a missing FMV doesn't abort a successful snipe add.

### Issue C — Fix `comic:buy` and `comic:snipe-add` skill descriptions (comic-pipeline)

In `buy.md` Step 3, fix the FMV command name:
- Change `gixen-cli fmv --batch` → `comic-fmv --batch`

In `snipe-add.md` "Add to Gixen" section, update the add command once Issues A and B are done:
- Change `--comic "{title}" --issue "{issue}" --year {year} --grade {grade_numeric}` to just `--locg-id {locg_id} --grade {grade_numeric}`
- Remove `--comic`, `--issue`, `--year`, `--locg-variant-id` (the new endpoint doesn't need them; it resolves the comic via `locg_id`)

---

## Key files

| File | Repo | Role |
|---|---|---|
| `plugins/gixen-overlay/src/gixen_overlay/routes.py` | comic-pipeline | Add `POST /api/bids/{item_id}/link-fmv` endpoint here |
| `plugins/gixen-overlay/src/gixen_overlay/db.py` | comic-pipeline | `link_fmv_to_bid()` already implemented here |
| `plugins/gixen-overlay/src/gixen_overlay/models.py` | comic-pipeline | Add `LinkFmvRequest` model here |
| `.claude/skills/buy.md` | comic-pipeline | Fix FMV command name (Step 3) |
| `.claude/skills/snipe-add.md` | comic-pipeline | Fix add command flags |
| `cli.py` (lines 220–293) | gixen-cli (`~/Projects/gixen-cli`) | Add `--locg-id` + `--grade` to `add` command |
| `server/main.py` (line 749) | gixen-cli | `POST /api/bids` handler — no changes needed |

---

## Existing infrastructure that already works

- `upsert_fmv()` in `gixen_overlay/db.py` uses `ON CONFLICT … DO UPDATE SET … COALESCE(excluded.X, X)` — calling it with NULL values never overwrites existing FMV data. Safe.
- `link_fmv_to_bid()` in `gixen_overlay/db.py` correctly handles `is_primary=True`: demotes prior primary entries, inserts junction row, mirrors to `bids.fmv_id`. Already works.
- `POST /api/extract-comics` can retroactively link existing unlinked bids by parsing `ebay_title`. Useful as a recovery tool for bids added before Issues A+B land, but not a permanent fix (depends on title parsing for grade, not as reliable as explicit `locg_id` + `grade`).

---

## Dependency order

Issues A and B must land before Issue C. Issue A (overlay endpoint) can be done independently of Issue B (gixen-cli flags), but both need to be done for the full fix. Issue C (skills update) is a cleanup that finalizes the skill descriptions once the implementation exists.

---

## Implementation status (updated 2026-05-23)

All three issues from this document have been shipped:
- Issue A (`POST /api/bids/{item_id}/link-fmv`) — PER-115, commit `faac5fc`
- Issue B (`gixen-cli add --catalog-id --grade`) — PER-116, applied directly to gixen-cli main
- Issue C (skill file corrections) — PER-117, commit `5aa3558`

**Follow-up finding:** Using `POST /api/extract-comics` as a recovery tool after these fixes can still cause null FMV when eBay titles arrive in ALL-CAPS. `upsert_comic` uses exact-match SQL on `title`, so "THE MIGHTY THOR" creates a duplicate row instead of matching "The Mighty Thor". This creates a second category of FMV stubs. See `docs/solutions/database-issues/fmv-stub-row-case-mismatch-2026-05-23.md` and PER-120, PER-123, PER-124.
