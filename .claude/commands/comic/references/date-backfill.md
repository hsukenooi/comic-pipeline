# Release-date backfill (LOCG bulk import)

Loaded **on demand** by `/comic:collection-sync` Step 2b only when dateless rows remain.
Keep it out of the main skill body — it's the rare, token-heavy fallback path.

## Why this exists

LOCG's Bulk Import **hangs / times out at 0%** when an upload batch is **all-dateless**
(a single blank-date row matches fine by series+title, but a whole batch of them spins
the importer indefinitely — see `docs/solutions/integration-issues/locg-bulk-import-sync-learnings-2026-06-17.md`).
Win-sourced rows are the usual offender: when record-win resolves a series from the
existing store it skips Metron, stamps a `{year}-01-01` placeholder, and the export
**blanks** that placeholder (a wrong Jan-1 reads as "Not Found"). Result: dateless rows.

The durable fix is in code (record-win should fetch a real Metron date even when the
series pre-exists — BUI-210). This doc is the **manual fallback** until that lands, and
the last-resort tail afterward.

## What "Release Date" must be

- Column format: **`YYYY-MM-01`** (LOCG's convention is the 1st of the cover month).
- Use the **cover date** of the **original first printing** — not a reprint/TPB/collected
  edition, not the on-sale date.
- **Tolerance:** LOCG silently corrects a *roughly-right* date to its own canonical value
  (observed: sent `1969-05-01`, stored `1969-02-11`). So right **year** + approximately
  right **month** is enough. What fails:
  - a **placeholder** `YYYY-01-01` (non-Metron stamp) → "Not Found",
  - a **wrong-year** date → "Not Found",
  - a **blank** date → matches *individually*, but an **all-blank batch hangs**.

## Backfill, cheapest tier first

**Tier 1 — let record-win populate it (no work here).** Once BUI-211 lands, dateless
rows shouldn't reach the sync. If they do, continue.

**Tier 2 — deterministic, near-zero tokens:**
- **Cadence:** for a **consecutive run**, take one verified anchor issue's cover date and
  add 1 month per issue (1st-of-month). No network, no agents. Example: anchor
  `Uncanny X-Men #300 = 1993-05-01` ⇒ #301 `1993-06-01`, #302 `1993-07-01`, #303
  `1993-08-01`. Works only where issues are genuinely monthly and consecutive — verify
  the anchor and spot-check the last issue.
- **Metron** (`packages/locg-cli/src/locg/metron.py`, creds in `~/.gixen-server/.env`):
  reliable **only** when the series name + year you pass match Metron's catalog. A naive
  `lookup_issue("The X-Men","59",1970)` returned a **2005 reprint** — pass the correct
  year and expect misses on vintage/aliased series. Rate-limited to ~20 req/min.

**Tier 3 — web research (expensive; only the residual; isolate it):**
- Dispatch a **structured-output sub-agent** that returns *only* a date table — never
  inline a verbose research transcript into the sync context.
- Prompt rules that matter (they prevent the reprint trap):
  1. State the **exact series + volume** (e.g. "Uncanny X-Men, Marvel Vol. 1, the 1963
     run, originally *The X-Men*, renamed at #142"). The X-Men masthead split (#1–141 =
     *The X-Men*, #142+ = *Uncanny X-Men*) must be stated.
  2. Demand the **original single-issue first-printing** cover date — no reprints/TPBs.
  3. Output **`YYYY-MM-01`** + source + confidence; cross-check ≥1 source per issue.
  4. Use consecutive-run monotonicity as a built-in sanity check.
- Good sources: **Grand Comics Database (comics.org)** and **Mike's Amazing World of
  Comics**; **Marvel Fandom** for cross-checks. comics.org + fandom often **403** plain
  `WebFetch` — reach for the repo's **Firecrawl** skills/CLI (`firecrawl-search`,
  `firecrawl-scrape`) on those domains.

## Apply the dates

Fill the **already-generated CSV** (don't re-export — the export re-blanks placeholders),
matching on `Full Title`, then verify none remain blank:

```bash
python3 - <<'PY'
import csv
SRC="<the locg-bulk-import-*.csv from Step 2>"; DST=SRC.replace(".csv","-DATED.csv")
dates = { "Uncanny X-Men #300":"1993-05-01", "The X-Men #59":"1969-08-01", ... }  # Full Title -> YYYY-MM-01
rows=list(csv.DictReader(open(SRC))); fields=list(rows[0].keys())
for r in rows:
    if not r["Release Date"].strip() and r["Full Title"] in dates:
        r["Release Date"]=dates[r["Full Title"]]
csv.DictWriter(open(DST,"w",newline=""),fieldnames=fields).writerows([dict(zip(fields,fields))]+rows)  # header+rows
blank=[r["Full Title"] for r in rows if not r["Release Date"].strip()]
print("still blank:", blank or "NONE — every row dated")
PY
```

Upload the `-DATED` CSV. The re-import then propagates LOCG's canonical dates back into
the store, so each row stops being dateless permanently.

## Worked example (2026-06-23, this sync's 25 issues)

Verified cover dates from a past sync, reusable as Tier 2 cadence anchors for
the same runs (The X-Men, Thor, Uncanny X-Men, etc.) — moved to
`docs/reference/date-backfill-worked-example.md` to keep this fallback path
lean.
