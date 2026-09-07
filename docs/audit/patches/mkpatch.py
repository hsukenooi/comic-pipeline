#!/usr/bin/env python3
"""Build the prompt-audit patch from exact-string edits.

Each edit is (finding, relpath, old, new[, count]).  Every `old` must occur
exactly `count` times in the current file (asserted), so a drifted file fails
loudly instead of producing a wrong hunk.  Output: one unified diff per file,
concatenated into PATCH_OUT, then `git apply --check`ed.
"""
import os
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path("/Users/hsukenooi/Projects/comic-pipeline")
SCRATCH = Path(__file__).resolve().parent
NEW = SCRATCH / "new"
PATCH_OUT = SCRATCH / "2026-09-06-prompt-audit.patch"
INCLUDE_BUI569 = os.environ.get("INCLUDE_BUI569", "0") == "1"

EDITS: list[tuple] = []


def E(finding, path, old, new, count=1):
    EDITS.append((finding, path, old, new, count))


# ---------------------------------------------------------------- H1 / M4 / M1
E("H1", ".claude/agents/comic-identifier.md",
r"""Run all items in a single call:

```bash
cd ~/Projects/comic-pipeline/apps/ebay && python src/ebay_fetch.py --json <id1> <id2> ...
```

Also accepts full URLs:

```bash
cd ~/Projects/comic-pipeline/apps/ebay && python src/ebay_fetch.py --json https://www.ebay.com/itm/298217294954
```

Capture both stdout (JSON array) and stderr (error lines for dropped items). If the venv
is not set up:

```bash
cd ~/Projects/comic-pipeline/apps/ebay && pip install -e . -q
```
""",
r"""Run all items in a single call with the installed `ebay-fetch` console script (it also
accepts full URLs):

```bash
ebay-fetch --json <id1> <id2> ...
ebay-fetch --json https://www.ebay.com/itm/298217294954
```

Capture both stdout (JSON array) and stderr (error lines for dropped items). If
`ebay-fetch` is not on PATH, run `./scripts/install.sh` from the repo root.
""")

E("M4", ".claude/agents/comic-identifier.md",
r"""**Grade logic:**
- `grade_source` is `"missing"` → check the title first; if the grade appears explicitly
  in the title (e.g. "NM", "VF+", "FVF", "Fine+"), use it as the stated grade with a light
  note.
- Then check `grade_from_description` (BUI-148): if non-null, the script found a grade in
  the listing body — surface it as a weak, description-sourced grade with a light note, not
  "no grade."
- Reserve a strong ⚠️ only for listings with no grade signal *anywhere* (i.e. `grade` is
  null **and** `grade_from_description` is null).
""",
r"""**Grade logic:** `ebay-fetch` already checks item specifics, then the title, then the
description (`extract_grade`), so `grade_source` is the verdict — don't re-parse the title.
- `grade` non-null → the stated grade (note when `grade_source` is `"title"`).
- `grade` null but `grade_from_description` non-null (BUI-148) → a weak,
  description-sourced grade with a light note, not "no grade."
- Reserve a strong ⚠️ only for listings with no grade signal *anywhere* (`grade` null
  **and** `grade_from_description` null).
""")

if INCLUDE_BUI569:
    E("M1", ".claude/agents/comic-identifier.md",
r"""**Your final act is to send that table to `main` via `SendMessage`** — plain
text you return does not reach the caller on its own; going idle without this
call leaves the dispatcher with nothing to read (BUI-569).
""",
r"""Return the table as your final message — it is delivered to the dispatching
skill as your report.
""")

# ---------------------------------------------------------------- comic-grader
E("M8", ".claude/agents/comic-grader.md",
r"""1. FRONT COVER — color fading, dust shadow, soiling, stains, writing (see PRINT-LAYER RULE + WRITING RULE below — printed credits/signatures are NOT writing), fingerprints,""",
r"""1. FRONT COVER — color fading, dust shadow, soiling, stains, writing (classify per the PRINT-LAYER RULE below), fingerprints,""")

E("M8", ".claude/agents/comic-grader.md",
r"""Some single defects set a hard ceiling regardless of otherwise high condition. Before assigning a final grade, check for these ceilings and state the cap explicitly in your rationale. (Printed cover elements — creator credits, facsimile signatures, barcodes, price boxes — are NEVER grade-capping; see PRINT-LAYER RULE. Only an authentic post-print autograph can act as a writing defect.)""",
r"""Some single defects set a hard ceiling regardless of otherwise high condition. Before assigning a final grade, check for these ceilings and state the cap explicitly in your rationale. (Print-layer elements never cap — see PRINT-LAYER RULE.)""")

E("M9", ".claude/agents/comic-grader.md",
r"""2. Use the Read tool on every img-XX.jpg in the folder (read all N).
""",
r"""2. Use the Read tool on every img-XX.jpg in the folder (read all N). When a deciding detail is small or ambiguous — a corner, a staple, a suspected mark — crop and enlarge that region and Read the crop (PIL is available: `python3 -c "from PIL import Image; im=Image.open('img-03.jpg'); im.crop((x0,y0,x1,y1)).resize((1200,1200)).save('/tmp/crop.jpg')"`); a zoomed view beats guessing from the full frame.
""")

if INCLUDE_BUI569:
    E("M8+M1", ".claude/agents/comic-grader.md",
r"""9. Be rigorous — do NOT inflate. Grade only what you can see, and let coverage cap your confidence.
10. Send the OUTPUT FORMAT block(s) below to `main` via `SendMessage` — your final act. A sub-agent's plain-text return does not reach the caller on its own (BUI-569).
""",
r"""9. Return the OUTPUT FORMAT block(s) below as your final message — it is delivered to the dispatching skill as your report.
""")
else:
    E("M8", ".claude/agents/comic-grader.md",
r"""9. Be rigorous — do NOT inflate. Grade only what you can see, and let coverage cap your confidence.
10. Send the OUTPUT FORMAT block(s) below to `main` via `SendMessage` — your final act. A sub-agent's plain-text return does not reach the caller on its own (BUI-569).
""",
r"""9. Send the OUTPUT FORMAT block(s) below to `main` via `SendMessage` — your final act. A sub-agent's plain-text return does not reach the caller on its own (BUI-569).
""")

# ---------------------------------------------------------------- verify.md
E("H2", ".claude/commands/comic/verify.md",
r"""- **LOCG collection verification** (did the comic land in LOCG with the right state?) is handled by step 7 of `/comic:collection-add` — it runs inline in the same Playwright session and checks `in_collection`, `wish_removed`, and `db_linked`. This skill covers the bid→fmv→comic DB chain only.""",
r"""- **Collection-side verification** (did the win land in the collection store and clear pending?) belongs to `/comic:collection-add` (the record-win commit response) and `/comic:collection-sync` Step 6's post-import check. This skill covers the bid→fmv→comic DB chain only.""")

E("M12", ".claude/commands/comic/verify.md",
r"""## How to read this file (BUI-361, updated BUI-507)

**EXECUTOR CONTRACT** = run it; **ORCHESTRATOR NOTES** = the verdict ladder (meanings only — the endpoint returns the guidance text itself as of BUI-507, so `/comic:buy` Step 6 no longer reads this file at all). Standalone `/comic:verify` runs: do both, in order.""",
r"""## How to read this file

**EXECUTOR CONTRACT** = run it; **ORCHESTRATOR NOTES** = the verdict ladder (meanings only — the endpoint returns the per-verdict guidance text itself, and `/comic:buy` Step 6 reads it from its add-batch output without opening this file). Standalone `/comic:verify` runs: do both, in order.""")

E("M12", ".claude/commands/comic/verify.md",
r"""calls in one shot, so there's no shell-state to carry between blocks (the
BUI-352/BUI-375 trap this structurally removes):""",
r"""calls in one shot, so there is no shell state to carry between blocks:""")

E("M12", ".claude/commands/comic/verify.md",
r"""Before the Call block below, write this run's working list to `working_list.verify.json`, **unconditionally overwriting** any file already at that path:

```bash
cat > working_list.verify.json <<'EOF'""",
r"""Before the Call block below, write this run's working list to `working_list.verify.json` in the run's scratch dir (`comics_scratch_dir`, BUI-430 — never the repo working tree), **unconditionally overwriting** any file already at that path:

```bash
source "$(git rev-parse --show-toplevel)/scripts/comics-server.sh"
SCRATCH="$(comics_scratch_dir)" || exit 1
cat > "$SCRATCH/working_list.verify.json" <<'EOF'""")

E("M12", ".claude/commands/comic/verify.md",
r"""```bash
comics-api POST /api/comics/verify \
  -H 'content-type: application/json' \
  -d @working_list.verify.json || {""",
r"""```bash
source "$(git rev-parse --show-toplevel)/scripts/comics-server.sh"
SCRATCH="$(comics_scratch_dir)" || exit 1
comics-api POST /api/comics/verify \
  -H 'content-type: application/json' \
  -d @"$SCRATCH/working_list.verify.json" || {""")

E("M12", ".claude/commands/comic/verify.md",
r"""**Per-verdict guidance (BUI-507):** no longer duplicated here — the endpoint
returns a `guidance` string on every result (see EXECUTOR CONTRACT § Output).
Both this skill and `/comic:buy` Step 6 render that string directly.""",
r"""**Per-verdict guidance:** the endpoint returns a `guidance` string on every
result (BUI-507; see EXECUTOR CONTRACT § Output). Both this skill and
`/comic:buy` Step 6 render that string directly.""")

E("M12", ".claude/commands/comic/verify.md",
r"""- **End of `/comic:buy`** — Step 6. Since BUI-360 the verify call itself rides
  along with Step 5 (`gixen add-batch --verify`); since BUI-507 each row's
  embedded `verify.guidance` is server-provided, so Step 6 reads it straight
  off the JSON without opening this file. No executor dispatch, no second call.""",
r"""- **End of `/comic:buy`** — Step 6. The verify call rides along with Step 5
  (`gixen add-batch --verify`, BUI-360) and each row's embedded
  `verify.guidance` is server-provided (BUI-507), so Step 6 reads it straight
  off the JSON without opening this file.""")

# ---------------------------------------------------------------- root CLAUDE.md
E("H3", "CLAUDE.md",
r"""**One-time server seed** (run once on the Mac Mini): `mkdir -p ~/.comics-server/collection-store && cp data/locg/{collection,wish-list}.json ~/.comics-server/collection-store/`. `ids.json` stays local (only `locg lookup` uses it).""",
r"""The server store lives at `~/.comics-server/collection-store` on the Mac Mini (seeded once, June 2026 — never copy `data/locg/*.json` over it again; that local cache is stale, and recovery goes through `POST /api/comics/collection/{backup,restore}`). `ids.json` stays local (only `locg lookup` uses it).""")

E("M14", "CLAUDE.md",
r"""**Never use the bare `locg collection check` CLI directly for ownership checks — it reads the MacBook's local store, which is never seeded and always returns `not_in_cache`.**""",
r"""**Never use the bare `locg collection check` CLI directly for ownership checks — on either machine it reads a local store that is not the server's, so it returns a false `not_in_cache`.**""")

E("M15", "CLAUDE.md",
r"""; collapses `/comic:collection-add`'s old inline-merge + client-side mark-seen + separate status re-fetch into one atomic call)""",
r""" — one atomic call, so `/comic:collection-add` never merges, marks seen, or re-fetches status client-side)""")

E("M15", "CLAUDE.md",
r"""and `data/locg/` is now gitignored (a local-only working cache, not repo-versioned)""",
r"""and `data/locg/` is gitignored (a local-only working cache, not repo-versioned)""")

E("M15", "CLAUDE.md",
r"""BUI-49 chose a pure rename (Option A), not splitting live-cancel vs completed-sweep into distinct statuses.""",
r"""BUI-49 deliberately kept a single tombstone status rather than splitting live-cancel from completed-sweep.""")

# ---------------------------------------------------------------- locg-cli CLAUDE.md
E("H5", "packages/locg-cli/CLAUDE.md",
r"""CLI for [League of Comic Geeks](https://leagueofcomicgeeks.com). Scraping-based (no official API).
""",
r"""CLI for [League of Comic Geeks](https://leagueofcomicgeeks.com). Scraping-based (no official API).

> **LOCG live access is blocked (standing state since 2026-08-10).** locg.com rejects all
> programmatic/agent traffic, so the scraping commands below (`search`, `series`, `find`,
> `comic`, `releases`, `login`, `add`, `remove`, `update`, `check`, and the list views) do
> not work from an agent session. Do not attempt or suggest automating LOCG. The collection
> and wish-list are read and written through the comics server (`/api/comics/*`), and the
> LOCG sync is the manual CSV round-trip in `/comic:collection-sync`. The Metron-backed
> commands (`creator-run`, `resolve-year`) and the server-backed `collection check-batch` /
> `collection audit-pending` are unaffected.
""")

E("M2", "packages/locg-cli/CLAUDE.md",
r"""Run with `PYTHONPATH=src python3 -m locg <command>` (or `locg <command>` if installed).""",
r"""Run with `locg <command>` (installed by `./scripts/install.sh`; from a bare checkout, `PYTHONPATH=src python3 -m locg <command>`).""")

E("M15", "packages/locg-cli/CLAUDE.md",
r"""`wish-list.json`, which are a local-only working cache, gitignored since
BUI-87/93 — the comics server on the Mac Mini is the source of truth for
collection and wish-list state. **As of BUI-476, `collection import` and
record-win (the mutating commands) no longer honor that fallback** — they
require `LOCG_DATA_DIR` set explicitly (e.g.""",
r"""`wish-list.json`, which are a local-only working cache (gitignored,
BUI-87/93) — the comics server on the Mac Mini is the source of truth for
collection and wish-list state. **The mutating commands — `collection import`
and record-win — do not honor that fallback (BUI-476)**: they require
`LOCG_DATA_DIR` set explicitly (e.g.""")

E("M15", "packages/locg-cli/CLAUDE.md",
r"""whichever store the read-side precedence would have resolved. The
`<repo>/data/locg` fallback now applies to reads only.""",
r"""whichever store the read-side precedence would have resolved. The
`<repo>/data/locg` fallback applies to reads only.""")

E("M15", "packages/locg-cli/CLAUDE.md",
r"""# Append a title to the local wish-list cache (no LOCG round-trip).
# As of BUI-208, `locg collection import` no longer touches wish-list.json —
# it is the single source of truth for wish state, so this local add (marked
# source: local) survives a subsequent import; only a server-side removal or
# a manual re-seed changes it.""",
r"""# Append a title to the local wish-list cache (no LOCG round-trip).
# `locg collection import` never touches wish-list.json (BUI-208) — it is the
# single source of truth for wish state, so this local add (marked
# source: local) survives a subsequent import; only a server-side removal or
# a manual re-seed changes it.""")

E("H4", "packages/locg-cli/CLAUDE.md",
r"""Session cookies are stored at `~/.config/locg/cookies.json`. Sessions can expire server-side; if you get "Session expired", run `locg login` again.""",
r"""The session and `cf_clearance` cookies persist in the Playwright profile at `~/.config/locg/playwright-profile/`. Sessions can expire server-side; if you get "Session expired", run `locg login` again.""")

E("H4", "packages/locg-cli/CLAUDE.md",
r"""- `pytest` (test only)

**Note:** Existing `~/.config/locg/cookies.json` is no longer read. Run `locg login` once after upgrading to populate the new Playwright profile.""",
r"""- `pytest` (test only)""")

# ---------------------------------------------------------------- gixen-cli CLAUDE.md
E("H9", "packages/gixen-cli/CLAUDE.md",
r"""```bash
# Run unit tests (mocked, no credentials needed)
pytest tests/test_gixen_client.py
pytest tests/test_server_api.py
pytest tests/test_server_db.py
pytest tests/test_add_batch.py
pytest tests/test_cli_add_batch.py
pytest tests/test_cli_build_batch.py
pytest tests/test_cli_record_win_prep.py
pytest tests/test_ebay_fallback.py
pytest tests/test_log_config.py
pytest tests/test_record_win_prep.py
pytest tests/test_skill_migration.py
pytest tests/test_standalone_server.py

# Run integration tests (requires GIXEN_USERNAME and GIXEN_PASSWORD in .env)
pytest -m integration

# Run the CLI (direct mode)
python cli.py list
python cli.py add <item_id> <max_bid> [--offset 6] [--group 0]
python cli.py edit <item_id> <max_bid>
python cli.py remove <item_id>
python cli.py purge

# Run the CLI (thin-client mode — set COMICS_SERVER_URL in .env)
python cli.py add <item_id> <max_bid> [--offset 6] [--group 0]
python cli.py add-batch <rows.json> [--verify] [--json-out results.json]  # BUI-360: batch add, server-mode only (no direct-Gixen fallback)
python cli.py build-batch <brief.json> <working_list.json> [--overrides overrides.json] [--out rows.json]  # BUI-435: build add-batch's rows.json deterministically
python cli.py sync                  # pull latest Gixen state into server DB

# Run the server (development)
uvicorn server.main:app --reload

# Deploy the server on Mac Mini
bash server/install.sh
```""",
r"""```bash
# Run unit tests (mocked, no credentials needed) — the whole suite, from this directory
uv run pytest -m "not integration"

# Run integration tests (requires GIXEN_USERNAME and GIXEN_PASSWORD in .env)
uv run pytest -m integration

# The CLI is the `gixen` console script (installed by ./scripts/install.sh).
# Direct mode (COMICS_SERVER_URL unset — talks to Gixen itself)
gixen list
gixen add <item_id> <max_bid> [--offset 6] [--group 0]
gixen edit <item_id> <max_bid>
gixen remove <item_id>
gixen purge

# Thin-client mode (COMICS_SERVER_URL set in .env or the environment)
gixen add <item_id> <max_bid> [--offset 6] [--group 0]
gixen add-batch <rows.json> [--verify] [--json-out results.json]  # BUI-360: batch add, server-mode only (no direct-Gixen fallback)
gixen build-batch <brief.json> <working_list.json> [--overrides overrides.json] [--out rows.json]  # BUI-435: build add-batch's rows.json deterministically
gixen sync                  # pull latest Gixen state into server DB

# Run the server (development)
uvicorn server.main:app --reload

# Deploy on the Mac Mini after a merge: ./scripts/deploy.sh (BUI-612) reinstalls every
# console script, syncs the workspace, kickstarts the launchd job, and asserts the deployed
# SHA. server/install.sh is only the first-time LaunchAgent installer.
```""")

# ---------------------------------------------------------------- snipe-add.md
E("H8", ".claude/commands/comic/snipe-add.md",
r"""## Role in /comic:buy (BUI-360 / BUI-361)

The orchestrated buy flow does **not** dispatch this skill as a sub-agent:
`/comic:buy` Step 5 calls `gixen add-batch` inline, with this skill's pre-flight
bid sanity check and user approval gate folded into that step and the BUI-168
failure semantics enforced by the CLI itself. This file therefore has **no
EXECUTOR CONTRACT / ORCHESTRATOR NOTES split** (unlike `collection-check.md` and
`verify.md`, BUI-361) — with no orchestrated dispatch, an ORCHESTRATOR NOTES
section would have no reader. The whole file is the contract for its two
remaining callers:

- a **standalone `/comic:snipe-add`** invocation (user-approved item_ids + max
  bids, outside the buy flow), and
- an **ad-hoc dispatched executor** (e.g. "add one more snipe" after a run) —
  point it at this file; everything it must do is here.

Both callers use the same routing rule as `/comic:buy` (BUI-436): **2+
approved items with the comics server up → `gixen add-batch`** (§ Add to
Gixen) is the primary path — the hand-looped per-item `gixen add` + BUI-168
prose discipline is now scoped to a single add or the direct-Gixen fallback,
where `add-batch` (server-mode-only) doesn't apply.""",
r"""## Role in /comic:buy

The orchestrated buy flow does **not** dispatch this skill as a sub-agent:
`/comic:buy` Step 5 calls `gixen add-batch` inline (BUI-360), with this skill's
pre-flight bid sanity check and user approval gate folded into that step and
the BUI-168 failure semantics enforced by the CLI itself. This file is the whole
contract for its two callers:

- a **standalone `/comic:snipe-add`** invocation (user-approved item_ids + max
  bids, outside the buy flow), and
- an **ad-hoc dispatched executor** (e.g. "add one more snipe" after a run) —
  point it at this file; everything it must do is here.

Both callers use the same routing rule as `/comic:buy` (BUI-436): **2+
approved items with the comics server up → `gixen add-batch`** (§ Add to
Gixen). The per-item `gixen add` loop and its BUI-168 discipline apply only to
a single add or the direct-Gixen fallback, where `add-batch` (server-mode-only)
doesn't exist.""")

E("H8", ".claude/commands/comic/snipe-add.md",
r"""**1. Server health**

Before doing anything else, verify the server is configured and up.

Check that `COMICS_SERVER_URL` is set:

```bash
echo "${COMICS_SERVER_URL:-UNSET}"
```

If it is not set, **stop immediately** with: "`COMICS_SERVER_URL` is not set. Snipes cannot be recorded in the DB. Set the variable and confirm the server is running before continuing." Do not proceed.

Verify the server is responding:

```bash
curl -sf "$COMICS_SERVER_URL/health"
```

If this fails or returns non-200, **stop immediately** with: "The comics server at `$COMICS_SERVER_URL` is not responding. Snipes cannot be recorded in the DB. Confirm the server is running before continuing." Do not proceed.""",
r"""**1. Server health**

Before doing anything else, resolve and health-gate the comics server through
`comics-api` (BUI-510 — it resolves the URL from the environment or the
Mac Mini / MacBook hostname, checks `/health`, and exits non-zero on either
failure):

```bash
comics-api GET /health >/dev/null
```

If that fails, **stop** with: "The comics server is not reachable. Snipes cannot
be recorded in the DB. Confirm the server is running before continuing." The
only exception is direct-Gixen mode (§ Fallback invocations), which the user
must ask for explicitly — an unrecorded snipe is invisible to FMV linking,
`/comic:verify`, and `/comic:collection-add`.""")

E("H8", ".claude/commands/comic/snipe-add.md",
r"""2. **Re-check server health** before the next item (`curl -sf "$COMICS_SERVER_URL/health"`).""",
r"""2. **Re-check server health** before the next item (`comics-api GET /health >/dev/null`).""")

# ---------------------------------------------------------------- buy.md
E("H7", ".claude/commands/comic/buy.md",
r"""both feed FMV accuracy (BUI-161). This replaces the old "non-Marvel/DC only" rule, which disabled the BUI-315 Marvel qualifier on every Marvel book in the batch: `ebay-sold-comps` already applies per-publisher policy centrally""",
r"""both feed FMV accuracy (BUI-161). `ebay-sold-comps` applies per-publisher policy centrally""")

E("M16", ".claude/commands/comic/buy.md",
r"""output (Steps 2, 3, 5, 6). No step dispatches a leaf skill's executor contract
to a sub-agent: post-BUI-504 the collection check is one CLI call, and
post-BUI-507 verification rides along in Step 5's output and is read as JSON at
Step 6. Leaf skills stay usable standalone — some (e.g. `verify.md`) still carry
a self-contained executor contract for that case; the buy flow just doesn't
route through it.""",
r"""output (Steps 2, 3, 5, 6). No step dispatches a leaf skill's executor contract
to a sub-agent: the collection check is one CLI call (BUI-504), and
verification rides along in Step 5's output and is read as JSON at Step 6
(BUI-507). Leaf skills stay usable standalone — some (e.g. `verify.md`) carry
a self-contained executor contract for that case; the buy flow doesn't route
through it.""")

E("M16", ".claude/commands/comic/buy.md",
r"""Step 4 owns the no-re-fetch rule and the data-age threshold that now conditions it (BUI-359/BUI-567).""",
r"""Step 4 owns the no-re-fetch rule and the data-age threshold that conditions it (BUI-359/BUI-567).""")

E("M16", ".claude/commands/comic/buy.md",
r"""`COMICS_SERVER_URL` is already resolved in Step 0, so this actually runs:""",
r"""`COMICS_SERVER_URL` is resolved in Step 0, so this call reaches the server:""")

E("H13", ".claude/commands/comic/buy.md",
r"""through collection-check.md's § Step 4
decision gate — the user resolves each;""",
r"""through collection-check.md's § Decision
gate — the user resolves each;""")

E("M16", ".claude/commands/comic/buy.md",
r"""- **Fresh run (elapsed < ~2h):** use those columns as-is here — do **not**
  re-fetch listings or re-ask the identifier subagent for prices. This is
  BUI-359's original rule, unchanged for the common case where the whole run
  completes in minutes.
- **Stale run (elapsed ≥ ~2h):** BUI-359's no-re-fetch rule no longer applies
  as-is. Before presenting max bids, either:""",
r"""- **Fresh run (elapsed < ~2h):** use those columns as-is here — do **not**
  re-fetch listings or re-ask the identifier subagent for prices (the common
  case: the whole run completes in minutes).
- **Stale run (elapsed ≥ ~2h):** before presenting max bids, either:""")

E("M16", ".claude/commands/comic/buy.md",
r"""This step calls `gixen add-batch` inline (BUI-360) instead of dispatching `snipe-add.md`'s per-item loop — the BUI-168 mid-batch failure semantics""",
r"""This step calls `gixen add-batch` inline (BUI-360) — the BUI-168 mid-batch failure semantics""")

E("M16", ".claude/commands/comic/buy.md",
r"""and appends a verdict per row (see Step 6) in the same call — this is what collapses the old separate Step 6 sub-agent into this one invocation.""",
r"""and appends a verdict per row (see Step 6) in the same call, so Step 6 needs no second call.""")

E("M16", ".claude/commands/comic/buy.md",
r"""the server now classifies an unpurged sibling `REMOVED` on its own once its auction ends""",
r"""the server classifies an unpurged sibling `REMOVED` on its own once its auction ends""")

E("M16", ".claude/commands/comic/buy.md",
r"""Step 5's `--verify` already appended a `verify` verdict — and, as of BUI-507, a server-provided `guidance` string — to every landed row in its JSON output. This step is now just **interpreting that data**: no second call, and no need to read `verify.md`.""",
r"""Step 5's `--verify` already appended a `verify` verdict and a server-provided `guidance` string (BUI-507) to every landed row in its JSON output. This step is **interpreting that data**: no second call, and no need to read `verify.md`.""")

# ---------------------------------------------------------------- fmv.md
E("H7", ".claude/commands/comic/fmv.md",
r"""**Pass `publisher` whenever you know it — including Marvel and DC (BUI-566).** This corrects the older "only for non-Marvel/DC titles" advice, which silently disabled a shipped fix: `ebay-sold-comps` decides per-publisher""",
r"""**Pass `publisher` whenever you know it — including Marvel and DC (BUI-566).** `ebay-sold-comps` decides per-publisher""")

E("M10", ".claude/commands/comic/fmv.md",
r"""A two-token `dc comics` measurably narrows recall (Batman #232: 34 comps → 12; Detective #400: 38 → 21), so DC short-circuits to no qualifier at all (BUI-315/BUI-321). Passing `DC` is a safe no-op, not a regression — the query is byte-for-byte the same as omitting it.""",
r"""A two-token `dc comics` qualifier measurably narrowed recall (it roughly halved the pool on Batman #232 and Detective #400, measured on SerpApi — BUI-315/BUI-321), so DC short-circuits to no qualifier at all. Passing `DC` is a safe no-op — the query is byte-for-byte the same as omitting it.""")

E("M10", ".claude/commands/comic/fmv.md",
r"""Also note BUI-315's recall measurements predate the BUI-545 provider switch — they were taken on SerpApi, and the qualifier's effect on sold-comps.com is unmeasured.""",
r"""The qualifier's effect on sold-comps.com (the current primary) is unmeasured.""")

E("M10", ".claude/commands/comic/fmv.md",
r"""before the title ever reaches `ebay-sold-comps`. Real incident: `"The Amazing Spider-Man #50"` alongside `issue: "50"` built the doubled, malformed query `"The Amazing Spider-Man #50 50"` — 0 results on every tier (ASM #50, 2026-07-13). `ebay-sold-comps`' `build_query`""",
r"""before the title ever reaches `ebay-sold-comps` (otherwise `"The Amazing Spider-Man #50"` plus `issue: "50"` builds the doubled query `"The Amazing Spider-Man #50 50"` and returns nothing). `ebay-sold-comps`' `build_query`""")

E("M10", ".claude/commands/comic/fmv.md",
r"""**Hand-priced rows (BUI-533, widened BUI-759, made durable BUI-769):**""",
r"""**Hand-priced rows (BUI-769):**""")

E("M10", ".claude/commands/comic/fmv.md",
r"""**When you hand-price a row, POST `fmv_provenance: "hand"` — the notes prefix is no longer the claim (BUI-769).** The prefix stays supported for one release so nothing already stored loses protection, but it fails open to any reword (`OVERRIDE:`, `priced by hand`, a translated phrase), and that failure costs a human's priced band replaced by the pooled answer they already rejected. The column cannot be lost to a reword, and a mistyped claim now **422s loudly** instead of silently failing open — `fmv_provenance` accepts only `hand` or `machine`. Keep writing a descriptive note as well; it is the reasoning, which is the part that cannot be recomputed.""",
r"""**When you hand-price a row, POST `fmv_provenance: "hand"` (BUI-769).** The column is the claim: it cannot be lost to a reworded note, and a mistyped value **422s loudly** — `fmv_provenance` accepts only `hand` or `machine`. (Legacy rows are still recognised by the `hand`/`manual` notes prefix, which fails open to any reword — don't rely on it for new rows.) Keep writing a descriptive note as well; it is the reasoning, which is the part that cannot be recomputed.""")

E("H6", ".claude/commands/comic/fmv.md",
r"""**This "one batch" advice is safe at any batch size again (BUI-701).** It briefly wasn't. `ebay-sold-comps` sits *exactly* at sold-comps.com's 60 req/min ceiling at steady state (4 workers × ~4s responses = 60/min, zero headroom), so a single large-enough batch could itself burst past the ceiling and tip into a self-reinforcing 429 storm — an instant 429, retried on the old 2s/4s/8s schedule, fed straight back into the still-saturated window. That is exactly what happened 2026-08-07: one 202-book batch, run as one call the way this section already recommended, short-circuited 109 queries and killed the back half of the run. The emergency workaround was the *opposite* of this section's advice — manually chopping the batch into 4 runs of 10 books with 45s pauses between them (36/36 fresh, zero 429s) — which only worked around the gap, at the cost of re-arming the breaker's trip cost on every chunk it was trying to avoid. BUI-701 closed the actual gap instead of leaning on that workaround: `sold_comps.py` now paces its own sold-comps.com dispatch rate in code (~48 req/min, ~20% headroom under the ceiling, shared across all worker threads) and gives a 429 specifically a much longer, jittered backoff instead of the generic schedule, so the window-fitting the manual chunking used to buy by hand now happens automatically *inside* a single invocation, at any batch size — a 200-book batch is proven (a mocked-clock, fake-provider test with no live calls) to complete with zero terminal 429s. **Don't manually chunk a batch to dodge 429s anymore** — it no longer helps, and it still pays the breaker-reset cost this section warns against.

> **Incident (2026-07-31):** ~107 book-queries across four runs in a few minutes — a 66-book batch, a 6-book re-run, a 5-book hypothesis test, then a 30-book fetch — tripped **both** providers' breakers. The last fetch returned `comps=0` for 27 of 30 books (the three with data were cache hits), and 30 books were dropped from the run.""",
r"""**One batch is safe at any size (BUI-701).** `sold_comps.py` paces its own sold-comps.com dispatch in code (~48 req/min, ~20% under the provider's 60 req/min ceiling, shared across all worker threads) and gives a 429 a long, jittered backoff, so a single invocation fits the rate window on its own (a 200-book batch is covered by a mocked-clock test). **Don't chunk a batch by hand to dodge 429s** — it doesn't help, and it pays the breaker-reset cost this section warns against.""")

E("M10", ".claude/commands/comic/fmv.md",
r"""the pool `comic-fmv` already priced is the pool (the FF #16 run, 2026-07-16, burned ~11 tool calls re-deriving it by hand to nudge the bid factor 0.60→0.70, for zero change to the FMV).""",
r"""the pool `comic-fmv` already priced is the pool, and re-deriving it by hand cannot change the verdict.""")

# ---------------------------------------------------------------- grade.md
E("M11", ".claude/commands/comic/grade.md",
r"""Run the downloader script (BUI-279 — extracted from this skill so the OAuth + Browse API logic isn't re-read into context on every grade run; same OAuth flow, same `get_item_by_legacy_id` calls, same image-download logic):

```bash
python3 ~/Projects/comic-pipeline/apps/ebay/src/grade_photos.py 178057470740 178057488707 ...
```""",
r"""Run the installed `grade-photos` console script (BUI-279 — it owns the OAuth, Browse API, and download logic so none of it is read into context):

```bash
grade-photos 178057470740 178057488707 ...
```""")

E("M11", ".claude/commands/comic/grade.md",
r"""**BUI-440:** when `--workdir` is not passed, each run gets its own fresh directory under `/tmp/comic-grading` (not the bare shared root) so a prior run's `comic-N` dirs never leak into a smaller later run, and two overlapping runs (a `/comic:buy` run + a standalone `/comic:grade` run) never collide on `comic-1`/`comic-2`.""",
r"""When `--workdir` is not passed, each run gets its own fresh directory under `/tmp/comic-grading` (BUI-440) so a prior run's `comic-N` dirs never leak into a smaller later run, and two overlapping runs never collide on `comic-1`/`comic-2`.""")

E("M11", ".claude/commands/comic/grade.md",
r"""`grade_photos.py`""", r"""`grade-photos`""", count=2)

E("M11", ".claude/commands/comic/grade.md",
r"""**Cheap books → batch them (U9).**""", r"""**Cheap books → batch them.**""")

E("M11", ".claude/commands/comic/grade.md",
r"""(grade each book on the **absolute** CGC scale; BUI-81 U9)""",
r"""(grade each book on the **absolute** CGC scale; BUI-81)""")

# ---------------------------------------------------------------- collection-check.md
E("H13", ".claude/commands/comic/collection-check.md",
r"""Check whether identified comics are already in your collection. As of BUI-504
the whole check is **one CLI call** — `locg collection check-batch` mechanizes
what used to be a ~450-line prose executor (server resolve → health gate →
status → batch check → stale-cache downgrade → the false-match flags). The
skill is now just the input shape, the decision gate, and carry-forward.""",
r"""Check whether identified comics are already in your collection. The whole check
is **one CLI call** — `locg collection check-batch` (BUI-504) does the server
resolve, health gate, status read, batch check, stale-cache downgrade, and the
false-match flags. This skill is the input shape, the decision gate, and
carry-forward.""")

E("H13", ".claude/commands/comic/collection-check.md",
r"""at the Step 4 decision gate below — recoverable.""",
r"""at the decision gate below — recoverable.""")

E("H13", ".claude/commands/comic/collection-check.md",
r"""## Step 4: Decision gate""", r"""## Decision gate""")

E("M14", ".claude/commands/comic/collection-check.md",
r"""> Never use the `locg collection check` CLI (no `-batch`) for ownership — it
> reads the MacBook's local store, which is never seeded and always returns
> `not_in_cache`. `check-batch` and the curl above both hit the Mac Mini's
> authoritative store.""",
r"""> Never use the `locg collection check` CLI (no `-batch`) for ownership — on
> either machine it reads a local store that is not the server's, so it
> returns a false `not_in_cache`. `check-batch` and the curl above both hit
> the Mac Mini's authoritative store.""")

# ---------------------------------------------------------------- wishlist-add.md
E("H10", ".claude/commands/comic/wishlist-add.md",
r"""On confirmation, add every issue in **one** request (BUI-447) — this replaces
the old per-issue `curl` loop, which turned a 40-issue run into 40 sequential
POSTs. Build a single items list""",
r"""On confirmation, add every issue in **one** request (BUI-447), never a
per-issue loop. Build a single items list""")

E("H10", ".claude/commands/comic/wishlist-add.md",
r"""the Step 3 filter does, AND as of **BUI-387** the year is now **persisted** on
the wish entry (a separate `year` field). That persisted Cover Year is what lets""",
r"""the Step 3 filter does, AND the year is **persisted** on the wish entry (a
separate `year` field, BUI-387). That persisted Cover Year is what lets""")

E("H10", ".claude/commands/comic/wishlist-add.md",
r"""**Removing a wished issue:** the wish-list also has a DELETE endpoint (BUI-128),
so you no longer need to SSH into the Mac Mini to run `locg wish-list remove`:""",
r"""**Removing a wished issue:** use the wish-list DELETE endpoint (BUI-128) — never
SSH to the Mac Mini for `locg wish-list remove`:""")

E("H10", ".claude/commands/comic/wishlist-add.md",
r"""The wish-list endpoint is idempotent as of BUI-285 (a re-added series+issue is a
200 no-op returning `{"status": "exists", ...}`, not a duplicate row), but still
filter duplicates out up front — the client-side scan avoids N redundant POSTs
and keeps the "already wished" list accurate for the Step 6 report. Fetch the
wish-list **once** and scan it **in memory** (BUI-204), not re-fetched or
re-grepped per issue. A real wish-list is large (685 items in the motivating
run); a per-issue grep over that payload is the redundant work this step removes.""",
r"""The wish-list endpoint is idempotent (BUI-285: a re-added series+issue is a
200 no-op returning `{"status": "exists", ...}`, not a duplicate row), but still
filter duplicates out up front — the client-side scan avoids N redundant POSTs
and keeps the "already wished" list accurate for the Step 6 report. Fetch the
wish-list **once** and scan it **in memory** (BUI-204) — a real wish-list runs
to several hundred items, so a per-issue re-fetch or grep is the expensive part.""")

E("H10", ".claude/commands/comic/wishlist-add.md",
r"""via `comics-api` (BUI-510, `docs/conventions/comics-server-call.md`) — every
call below is independently self-resolving""",
r"""via `comics-api` (BUI-510, `docs/conventions/comics-server-call.md`) — every
call below is independently self-resolving""")  # no-op guard: keeps file in edit list

E("H10", ".claude/commands/comic/wishlist-add.md",
r"""Also resolve and health-gate the comics server (the wish-list now lives there)""",
r"""Also resolve and health-gate the comics server (the wish-list lives there)""")

E("H10", ".claude/commands/comic/wishlist-add.md",
r"""the repo file is no longer the source of truth (BUI-93)""",
r"""the repo file is not the source of truth (BUI-93)""")

E("M13", ".claude/commands/comic/wishlist-add.md",
r"""  collectibles — owning the reprint is NOT owning the base printing (the
  confirmed AMM #1 incident: *Absolute Martian Manhunter #1* read as owned off
  an owned "2nd Printing" row while the base sat wish-listed; unpatched, Step 3
  would have silently skipped wish-listing that explicitly wanted base
  printing). Put this issue in a THIRD bucket""",
r"""  collectibles — owning the reprint is NOT owning the base printing (e.g. an
  owned *Absolute Martian Manhunter #1* "2nd Printing" while the base is the
  one wanted). Put this issue in a THIRD bucket""")

E("M13", ".claude/commands/comic/wishlist-add.md",
r"""direction), but a false "skip" reproduces the AMM #1 incident (a missed wish
for a book actually wanted).""",
r"""direction), but a false "skip" silently loses a wish for a book actually
wanted.""")

E("M13", ".claude/commands/comic/wishlist-add.md",
r"""unstamped — safe, year-blind, exactly as before). **Never pass `year_began`
(BUI-129)** — it must be THIS issue's cover year, or the wish is mis-scoped.""",
r"""unstamped — safe, year-blind). It must be THIS issue's cover year, never
`year_began` (Step 3 owns the BUI-129 rule).""")

# ---------------------------------------------------------------- collection-sync.md
E("H12", ".claude/commands/comic/collection-sync.md",
r"""**No client-side split needed.** As of BUI-208 the export ships **only wins**
(`In Collection=1`) — the server's `generate_csv` *refuses* to emit any
`In Collection=0` row unless `?push_wishes=true` is explicitly requested. So
the Step 2 file **is** your wins file, and the default sync is structurally
incapable of deleting a collection book.""",
r"""The export ships **only wins** (`In Collection=1`, BUI-208) — the server's
`generate_csv` *refuses* to emit any `In Collection=0` row unless
`?push_wishes=true` is explicitly requested. So the Step 2 file **is** your
wins file, and the default sync is structurally incapable of deleting a
collection book.""")

E("H12", ".claude/commands/comic/collection-sync.md",
r"""**BUI-466: a Jan-1 date no longer hard-stops the sync by shape alone.** The
audit reads the collection store""",
r"""**A Jan-1 date is a hard-stop only when confirmed as a placeholder (BUI-466).**
The audit reads the collection store""")

E("H12-210", ".claude/commands/comic/collection-sync.md",
r"""`dateless_titles`). The durable fix is record-win populating dates (BUI-210);
until then, follow the tiered procedure in **`references/date-backfill.md`**
(cadence/Metron first, web-research sub-agent only for the residual). Fill the
dates into the already-generated CSV (don't re-export — the export re-blanks
placeholders), then continue.""",
r"""`dateless_titles`). record-win dates wins from Metron (BUI-210), so dateless
rows are the residual Metron could not supply; follow the tiered procedure in
**`references/date-backfill.md`** (cadence/Metron first, web-research sub-agent
only for the residual). Fill the dates into the already-generated CSV (don't
re-export — the export re-blanks placeholders), then continue.""")

E("H12", ".claude/commands/comic/collection-sync.md",
r"""**The unscoped POST (no body) no longer removes anything (BUI-266 foot-gun
guard)** — it returns a non-mutating dry-run preview. `{"confirm": true}` still
performs the original remove-every-conflict sweep, but that reintroduces the
decoy risk above — prefer scoped `names`.""",
r"""**An unscoped POST (no body) is a non-mutating dry-run preview (BUI-266
foot-gun guard).** `{"confirm": true}` performs a remove-every-conflict sweep,
but that reintroduces the decoy risk above — prefer scoped `names`.""")

E("H12", ".claude/commands/comic/collection-sync.md",
r"""constraint, not row count** — there is no row limit (the earlier "≤20 rows per
batch" belief was a misdiagnosis of incomplete/dateless rows, not batch size;
see `docs/solutions/integration-issues/locg-sync-unified-model-2026-06-22.md`).""",
r"""constraint, not row count** — there is no row limit (a hang blamed on batch
size is really incomplete/dateless rows; see
`docs/solutions/integration-issues/locg-sync-unified-model-2026-06-22.md`).""")

E("H12", ".claude/commands/comic/collection-sync.md",
r"""  `owned_duplicate_identities == 0`. **This is a separate hard-stop, and the
  arithmetic above cannot substitute for it.** On the 2026-07-27 sync the row
  count balanced to the row (`2902 + 46 − 3 = 2945`, exact) while the import
  had silently created a *second* owned row for 28 books: every duplicate is
  one `added` row, so the arithmetic counts it as expected growth and reports
  clean. `owned_duplicate_identities` counts titles carried by **two owned
  rows of any kind** (matched punctuation-, whitespace- and
  article-insensitively, and only when their release dates are compatible so
  two genuine volumes of one masthead aren't falsely paired). BUI-554 removed
  the `agent_win`-vs-`locg_export` partition this used to require: once a sync
  had round-tripped every pending win back through LOCG as an export row, that
  partition was empty and the check reported 0 while 60 identities collided —
  a vacuous pass indistinguishable from a clean one. Non-zero means the
  reconciler missed — those rows stay
  pending and the next sync re-uploads them, so it compounds. The warning
  names the affected titles.""",
r"""  `owned_duplicate_identities == 0`. **This is a separate hard-stop, and the
  arithmetic above cannot substitute for it:** every unreconciled duplicate is
  one `added` row, so the row count balances exactly while books quietly
  become owned twice (28 books on the 2026-07-27 sync).
  `owned_duplicate_identities` counts titles carried by **two owned rows of
  any kind** — no `agent_win`-vs-`locg_export` partition (BUI-554), matched
  punctuation-, whitespace- and article-insensitively, and only when their
  release dates are compatible so two genuine volumes of one masthead aren't
  falsely paired. Non-zero means the reconciler missed — those rows stay
  pending and the next sync re-uploads them, so it compounds. The warning
  names the affected titles.""")

E("H12", ".claude/commands/comic/collection-sync.md",
r"""  Counted over **all** rows, not owned rows — the owned-scoping is exactly why
  the three live collisions (all wish-side `Absolute Martian Manhunter`) sat
  invisible for months. Non-zero means the import can only ever reach the
  **last** row per identity, so the others can be duplicated but never updated.
  Its first reading is **3**, and the remedy — `collection_io.rekey_sweep` — is
  a separate user-gated operation, so blocking on it would stop every sync from
  the day it shipped (the BUI-563 lesson). Never fold it into""",
r"""  Counted over **all** rows, not owned rows — owned-only scoping hides
  wish-side collisions. Non-zero means the import can only ever reach the
  **last** row per identity, so the others can be duplicated but never updated.
  The remedy — `collection_io.rekey_sweep` — is a separate user-gated
  operation, so blocking on it would stop every sync (the BUI-563 lesson).
  Never fold it into""")

# ---------------------------------------------------------------- collection-add.md
E("M7", ".claude/commands/comic/collection-add.md",
r"""regardless of price (`REASON_MISSING_YEAR`, BUI-422/BUI-475 — a win's era
can't be confirmed without a year, and vintage no-year titles are
disproportionately prone to a downstream volume mis-resolution; BUI-422's
original `$25` price threshold was removed in BUI-475 after the server-side
auto-resolve it was meant to lean on was shown to fail open — see the
rationale doc). There is deliberately no confidence threshold —
`comic-identify`'s baseline confidence (0.5) would fire on nearly every real
title (BUI-354; rationale doc has the full story of both).""",
r"""regardless of price (`REASON_MISSING_YEAR`, BUI-422/BUI-475: a win's era
can't be confirmed without a year, and vintage no-year titles are
disproportionately prone to a downstream volume mis-resolution). There is
deliberately no price threshold and no confidence threshold —
`comic-identify`'s baseline confidence (0.5) would fire on nearly every real
title (BUI-354; the rationale doc has the full story of both).""")

E("M6", ".claude/commands/comic/collection-add.md",
r"""**Expected duration — a multi-minute wait here is normal, not a hang
(BUI-472).** `cmd_collection_record_win` paces Metron traffic to
`METRON_REQUESTS_PER_MINUTE` (20 req/min, BUI-465) and spends **up to** 3 HTTP
requests per win — `REQUESTS_RESOLVE_SERIES` (1) + `REQUESTS_ISSUE_IN_SERIES`
(1) for the R36 step-2 series lookup (or the BUI-210 date-only lookup;
mutually exclusive, never both), plus `REQUESTS_LOOKUP_ISSUE_DETAIL` (1,
publisher/variant) — so pacing alone costs up to `60s / 20 req/min × 3
requests ≈ 9s/win` in the worst case. **BUI-473: a run of wins from the SAME
series only pays `REQUESTS_RESOLVE_SERIES` once** — the resolved series is
cached for the rest of the batch, so every subsequent issue of that series
spends only `REQUESTS_ISSUE_IN_SERIES` + `REQUESTS_LOOKUP_ISSUE_DETAIL` (2
requests, ~6s/win). The **worst case is unchanged and still the number to
plan around**: a batch where every win is a genuinely distinct series gets no
reuse, so the bound below still holds. A transient Metron trip additionally
costs up to `METRON_MAX_TRANSIENT_TRIPS` (3) cooldowns of
`METRON_TRANSIENT_COOLDOWN_SEC` (60s) each. Worst case for a batch of N
wins: **N × 9s + 3 × 60s**. A 40-win batch: `40 × 9 + 180 = 540s (~9 min)` —
same-series batches (the common manual-series backlog shape) finish faster
than this bound in practice, but the timeout must still cover it.

The server side cannot stall on this — the blocking call runs off the event
loop via `asyncio.to_thread` (BUI-428) — so the only real risk is the
**calling harness's own Bash-invocation timeout** (the `curl` below has no
`--max-time` of its own, and shouldn't grow one — the call is slow on
purpose). **Invoke this bash block with an explicit timeout of 600000ms
(10 minutes)** — the harness's own maximum for a single Bash call (it
cannot be raised further), and comfortably covers the 40-win worst case
above with ~60s of margin. A batch large enough to exceed even that ceiling
(roughly N > 46 wins) can still time out at the harness level — see Step 3b
below for what to do when that happens.""",
r"""**Expected duration — a multi-minute wait here is normal, not a hang
(BUI-472).** `cmd_collection_record_win` paces Metron traffic to 20 req/min
(BUI-465) and spends up to 3 requests per win (series resolve, issue-in-series,
issue detail), so the worst case is **~9s per win plus up to 3 × 60s
transient-trip cooldowns** — about 9 minutes for 40 wins. Same-series runs
reuse the series resolve (BUI-473) and finish faster, but size the timeout on
the worst case.

The server side cannot stall on this — the blocking call runs off the event
loop via `asyncio.to_thread` (BUI-428) — so the only real risk is the
**calling harness's own Bash-invocation timeout** (the `curl` below has no
`--max-time` of its own, and shouldn't grow one — the call is slow on
purpose). **Invoke this bash block with an explicit timeout of 600000ms
(10 minutes)**, the harness's maximum for a foreground Bash call, which covers
~46 wins. For a larger batch run the block with `run_in_background` instead —
a background command is not subject to that ceiling and re-invokes you when it
exits. If a foreground call did time out, see Step 3b.""")

E("M6", ".claude/commands/comic/collection-add.md",
r"""# BUI-472: this call is slow ON PURPOSE (BUI-465 pacing) — do not add
# --max-time here. Worst case = batch_size x ~9s (pacing: 3 Metron requests
# per win / 20 req-per-min budget = 60/20*3 = 9s/win) + up to 3 x 60s
# transient-trip cooldowns (METRON_MAX_TRANSIENT_TRIPS x
# METRON_TRANSIENT_COOLDOWN_SEC). A 40-win batch: 40*9 + 3*60 = 540s. BUI-473:
# same-series wins only pay the full 3 requests on the FIRST issue of that
# series (2 requests, ~6s/win, thereafter) — this worst case assumes no
# reuse (every win a distinct series) and is still the right number to size
# the timeout on. The risk is the HARNESS's Bash-invocation timeout, not this
# curl — that must be set explicitly on the tool call that runs this block
# (see the prose above: 600000ms, the harness's max).""",
r"""# BUI-472: this call is slow ON PURPOSE (BUI-465 pacing) — do not add
# --max-time here; the timeout to set is the tool call's own (see the prose
# above: 600000ms, or run_in_background for a large batch).""")

E("M6", ".claude/commands/comic/collection-add.md",
r"""| Invoking Step 3's Bash block without an explicit long timeout | It can legitimately run several minutes (BUI-465 pacing); set the tool call's timeout to 600000ms (the harness max) or it may hit the harness's own default timeout mid-write (BUI-472). Same-series batches finish faster in practice (BUI-473 reuses one series resolution per series), but the worst-case bound — and so the 600000ms recommendation — is unchanged: it assumes no reuse |""",
r"""| Invoking Step 3's Bash block without an explicit long timeout | It can legitimately run several minutes (BUI-465 pacing); set the tool call's timeout to 600000ms, or use `run_in_background` for a batch above ~46 wins (BUI-472) |""")

E("M16", ".claude/commands/comic/collection-add.md",
r"""No separate status call needed (BUI-428 collapsed it into Step 3): read
`pending_push_count`/`oldest_pending_days` straight out of Step 3's response.""",
r"""No separate status call is needed: read `pending_push_count`/
`oldest_pending_days` straight out of Step 3's response (BUI-428).""")

# ---------------------------------------------------------------- calibration-report.md
E("H11", ".claude/commands/comic/calibration-report.md",
r"""## The signal, and why it changed (BUI-532)

> **Headline: confirmed win-based exceedance — `contested_win_margin` where
> it is non-null and `> 1`. Secondary: loss-based `overshoot`, always labeled
> a censored upper bound, never a literal "raise `fmv_high` by this factor"
> number. Still never raw `loss_count` or a win/loss rate.**

Losing is the *intended* outcome of the 80% (or 60%, on low confidence) bid
haircut: you deliberately bid below fair value to bargain-hunt, so you are
*designed* to lose most auctions. A book with a huge loss count is not
mispriced by that fact alone — it's the haircut working exactly as designed,
as long as those losses clear **at or below** `fmv_high`. **Do not rank or
surface a book on `loss_count` or a win/loss ratio** — that reintroduces the
exact deflation/mispricing trap this report exists to avoid (R4 in the plan).

**What changed in BUI-532, and why:** earlier versions of this skill banned
`contested_win_margin` outright ("never `contested_win_margin`"). That ban
conflated two different claims: "don't treat a big bargain-win as if it makes
a book *safer* or less urgent" (still true, see below) with "win data can
never be evidence of mispricing" (false — and the actual bug BUI-532 fixes).
`overshoot` is broken two ways:

1. **Right-censored.** On a LOST auction, the recorded `winning_bid` is a
   floor (often just our `max_bid` plus one bid increment) — gixen has no way
   to observe what the actual winner paid. The true clearing price was
   **higher** than that floor, so any single loss's ratio *understates* how
   far that auction really cleared above `fmv_high`.
2. **Confounded by a moving `fmv_high`.** The `fmv` row a bid links to holds
   the **current**, recomputed value, but `max_bid` was frozen at snipe time.
   26% of rows have `max_bid >= fmv_high` today (the book's `fmv_high` has
   since moved, usually down) — a loss on one of those rows registers
   `overshoot > 1` by construction, regardless of what actually happened in
   the auction. That's a measurement artifact, not market evidence.

These two effects pull in different directions on paper — censoring alone
would *understate* the true ratio, while the frozen-`max_bid` confound
*inflates* it for the rows it touches — so `overshoot` isn't a clean bound by
construction alone. BUI-527's back-test
(`apps/fmv/scripts/fmv_high_calibration.py`, reference only — do not fork a
second copy of this analysis) shows which effect wins in this dataset: the
raw/naive exceedance rate (the fraction of rows whose recorded ratio exceeds
1) fell from 39% to 19.4% once collapsed-point rows (BUI-528, fixed
2026-07-24) and the frozen-`max_bid` rows were excluded — the confound, not
the censoring, is doing most of the inflating here. So treat any `overshoot`
value this report shows as a **censored upper bound**: it is more likely to
overstate the real signal than to understate it.

Wins carry no such distortion: a WON row's `winning_bid` is the exact price
you paid (bounded by your own `max_bid`, never floored at it), so
`contested_win_margin` is the one field in this report backed by fully
observed, uncensored data. BUI-527's evidence across the current dataset —
n=114 wins, median clearing at 0.57x `fmv_high`, only 4.4% exceedance — is
exactly why a win that *does* clear above `fmv_high` is trustworthy: it is
rare, and it is real. **A row with `contested_win_margin > 1` is the
strongest evidence this report can produce that `fmv_high` is too low** —
rank it ahead of every row that only has loss-based `overshoot` behind it.

A low `contested_win_margin` (well below 1) is *not* a counter-signal to
chase, and does not make a row "safer" or lower-priority than another
Unconfirmed row — it just means that particular win was a bargain. The only
promotion this rule allows is ranking a row **up** when its win-based margin
**exceeds 1**; never rank, filter, or promote a row because its margin is
*low*.

Every other mention of this rule below (response shape, Present the results,
Common mistakes) is a one-line pointer back to this section, not a separate
restatement — if you're tempted to relax it further, come edit it here. If
you are editing this skill or the server-side aggregate (`calibration_report`
in `plugins/gixen-overlay/src/gixen_overlay/db.py`), re-read this section and
the Problem Frame in
`docs/plans/2026-07-04-001-feat-fmv-auction-outcome-feedback-plan.md` first.
The `calibration_report` docstring, sort order, and surfacing gate were
brought in line with this section's framing by BUI-543 (BUI-532 was a
doc-only ticket that rebased this skill's vocabulary but left the
server-side aggregate's docstring/sort/gate on the pre-BUI-532
"`overshoot`-is-the-ranking-key" framing — BUI-543 closed that gap). The
plan's R4 text (`docs/plans/2026-07-04-001-feat-fmv-auction-outcome-feedback-plan.md`)
predates both tickets and still reads as win/loss-*rate*-only; this section
remains the current, correct read of how R4 interacts with win-based
exceedance.

**BUI-543 update:** a book with **only wins** now surfaces here whenever
`contested_win_margin > 1` — the server admits a row on win-based exceedance
alone, with **no loss requirement at all**. Before BUI-543 the server-side
gate required at least one qualifying loss regardless of how strong a book's
win-based signal was, which meant a book that won every auction with
`contested_win_margin > 1` several times, but never lost `min_losses` times,
never surfaced even though that would have been the strongest possible
evidence of underpricing. That was a real gap; it is now closed. The only
case that still never appears is a book with **no resolved auctions at
all** (nothing to measure either signal from), or one whose losses all
cleared at or below `fmv_high` **and** whose wins (if any) cleared at or
below `fmv_high` too — i.e. neither admit path fired (the server-side R4
guard, unchanged).

**A book's *loss-based* signal still requires at least `min_losses` losses
in-window before it counts (default 2)** — a single loss, however far above
`fmv_high` it cleared, is one bidding-war outlier, not a persistent pattern,
so the loss-based path suppresses it as noise rather than ranking a book on
one data point. **`min_losses` governs only that loss-based signal, never
whether a row surfaces at all (BUI-543):** a row with a qualifying win margin
surfaces regardless of `min_losses`, including with zero losses. Pass
`min_losses` as a query param to tighten or loosen the loss-based floor (e.g.
`min_losses=3` for a stricter gate); it can never be used to relax the
loss-count-is-not-the-signal rule above, and it never gates the win-based
admit path.

Each row carries `win_backed` / `loss_backed` booleans (see "Response shape"
below) so you can tell which admit path fired without knowing the
`min_losses` value the call used.""",
r"""## The signal

> **Headline: confirmed win-based exceedance — `contested_win_margin` where
> it is non-null and `> 1`. Secondary: loss-based `overshoot`, always labeled
> a censored upper bound, never a literal "raise `fmv_high` by this factor"
> number. Never raw `loss_count` or a win/loss rate.**

Losing is the *intended* outcome of the 80% (or 60%, on low confidence) bid
haircut: you deliberately bid below fair value to bargain-hunt, so you are
*designed* to lose most auctions. A book with a huge loss count is not
mispriced by that fact alone — it's the haircut working exactly as designed,
as long as those losses clear **at or below** `fmv_high`. **Do not rank or
surface a book on `loss_count` or a win/loss ratio** — that reintroduces the
exact deflation/mispricing trap this report exists to avoid (R4 in the plan).

**Why wins carry the headline and losses cannot.** `overshoot` (the median
`winning_bid / fmv_high` over losses) is distorted two ways:

1. **Right-censored.** On a LOST auction the recorded `winning_bid` is a
   floor (often just our `max_bid` plus one increment) — gixen never observes
   what the winner actually paid, so a loss's ratio *understates* how far the
   auction really cleared above `fmv_high`.
2. **Confounded by a moving `fmv_high`.** The `fmv` row a bid links to holds
   the **current**, recomputed value, while `max_bid` was frozen at snipe
   time; a loss on a row whose `fmv_high` has since moved down registers
   `overshoot > 1` by construction. BUI-527's back-test
   (`apps/fmv/scripts/fmv_high_calibration.py`, reference only — do not fork
   a second copy) showed this confound, not the censoring, does most of the
   inflating, so treat any `overshoot` as a **censored upper bound** that is
   more likely to overstate the real signal than understate it.

Wins carry no such distortion: a WON row's `winning_bid` is the exact price
paid, so `contested_win_margin` (the median ratio over wins) is the one field
backed by fully observed data. Wins that clear above `fmv_high` are rare (4.4%
of 114 wins in BUI-527's dataset), which is exactly why one that does is
trustworthy. **A row with `contested_win_margin > 1` is the strongest evidence
this report can produce that `fmv_high` is too low** — rank it ahead of every
row that only has loss-based `overshoot` behind it.

A low `contested_win_margin` (well below 1) is *not* a counter-signal, and
does not make a row "safer" than an Unconfirmed row — it just means that win
was a bargain. The only promotion this rule allows is ranking a row **up**
when its win-based margin **exceeds 1**; never rank, filter, or promote a row
because its margin is *low*.

**Admit paths.** A book surfaces when *either* fires:

- **win-backed** — `contested_win_margin > 1`, with **no loss requirement**
  (a book that won every auction above `fmv_high` still surfaces); or
- **loss-backed** — at least `min_losses` losses in-window (default 2) with
  `overshoot > 1`. A single loss, however far above `fmv_high`, is one
  bidding-war outlier, not a pattern, so it is suppressed as noise.

`min_losses` governs only the loss-based path (pass it as a query param, e.g.
`min_losses=3`, to tighten it); it never gates the win-based path and can
never relax the loss-count-is-not-the-signal rule. Only a book with no
resolved auctions at all, or one whose losses *and* wins all cleared at or
below `fmv_high`, is omitted (the server-side R4 guard). Each row carries
`win_backed` / `loss_backed` booleans (see "Response shape" below) so you can
tell which path fired without knowing the `min_losses` the call used.

If you are editing this skill or the server-side aggregate (`calibration_report`
in `plugins/gixen-overlay/src/gixen_overlay/db.py`), re-read this section and
the Problem Frame in
`docs/plans/2026-07-04-001-feat-fmv-auction-outcome-feedback-plan.md` first;
the history of the metric rebase is in BUI-532 and BUI-543.""")

E("H11", ".claude/commands/comic/calibration-report.md",
r""""The signal, and why it changed (BUI-532)" """, r""""The signal" """, count=3)

E("H11", ".claude/commands/comic/calibration-report.md",
r"""see "The
  signal, and why it changed (BUI-532)" above""",
r"""see "The
  signal" above""")

E("H11", ".claude/commands/comic/calibration-report.md",
r"""see "The signal, and why it changed (BUI-532)"
  above.""",
r"""see "The signal"
  above.""")

E("H11", ".claude/commands/comic/calibration-report.md",
r"""Optional `min_losses` query param (default 2 — a book must have lost at least
this many times in-window to surface; see "The signal, and why it changed
(BUI-532)" above for why a single loss doesn't count):""",
r"""Optional `min_losses` query param (default 2 — the loss-based admit path needs
at least this many in-window losses; see "The signal" above):""")

E("H11", ".claude/commands/comic/calibration-report.md",
r"""One object per flagged `(issue, grade)`. As of BUI-543 the comics-server
response itself is already ordered win-backed-first (each tier sorted by its
own metric descending) — the API's own order now matches the headline
ranking. You still need to **partition into two labeled tiers** for""",
r"""One object per flagged `(issue, grade)`. The comics-server response is
ordered win-backed-first (each tier sorted by its own metric descending). You
still need to **partition into two labeled tiers** for""")

E("H11", ".claude/commands/comic/calibration-report.md",
r"""- `win_backed` (bool, **BUI-543**) — `true` iff""", r"""- `win_backed` (bool) — `true` iff""")

E("H11", ".claude/commands/comic/calibration-report.md",
r"""- `loss_backed` (bool, **BUI-543**) — `true` iff""", r"""- `loss_backed` (bool) — `true` iff""")

E("H11", ".claude/commands/comic/calibration-report.md",
r"""   descending (the server already returns this tier in this order as of
   BUI-543, but sort defensively rather than depend on it). This is the
   headline list (BUI-532/BUI-543): real money actually cleared above""",
r"""   descending (the server already returns this tier in this order, but sort
   defensively rather than depend on it). This is the headline list: real
   money actually cleared above""")

E("H11", ".claude/commands/comic/calibration-report.md",
r"""| Rendering the raw API order without labeled tiers | The API returns win-backed-first order as of BUI-543, but still render""",
r"""| Rendering the raw API order without labeled tiers | The API returns win-backed-first order, but still render""")

E("H11", ".claude/commands/comic/calibration-report.md",
r"""| Assuming a zero-loss book can never surface | Fixed in BUI-543 — a `win_backed: true` row surfaces regardless of `loss_count`, including 0.""",
r"""| Assuming a zero-loss book can never surface | A `win_backed: true` row surfaces regardless of `loss_count`, including 0.""")

E("H11", ".claude/commands/comic/calibration-report.md",
r"""Plan: `docs/plans/2026-07-04-001-feat-fmv-auction-outcome-feedback-plan.md` — BUI-288 (Issue C).
Metric rebase: BUI-532, evidenced by BUI-527's back-test
(`apps/fmv/scripts/fmv_high_calibration.py`, PR #330).
Server-side admit path + self-describing fields: BUI-543 (this file's framing was already
current from BUI-532; the server-side gate/sort/docstring and this file's stale
ordering/gap claims were brought into line with it).""",
r"""Plan: `docs/plans/2026-07-04-001-feat-fmv-auction-outcome-feedback-plan.md` — BUI-288 (Issue C).
Metric history: BUI-532 (win-based headline, evidenced by BUI-527's back-test in
`apps/fmv/scripts/fmv_high_calibration.py`, PR #330) and BUI-543 (server-side
win-backed admit path + the `win_backed`/`loss_backed` fields).""")

E("M5", ".claude/commands/comic/calibration-report.md",
r"""## Prerequisites

**`COMICS_SERVER_URL` must be set.** Set it once in `~/.zshrc`:

```bash
# MacBook (connects to Mac Mini over Tailscale)
export COMICS_SERVER_URL=http://mac-mini.tail9b7fa5.ts.net:8080

# Mac Mini (running locally)
export COMICS_SERVER_URL=http://localhost:8080
```

`GIXEN_SERVER_URL` is a deprecated alias — it still works but emits a
warning. Migrate to `COMICS_SERVER_URL`.""",
r"""## Prerequisites

`comics-api` resolves the comics server itself (`COMICS_SERVER_URL` if set,
else the Mac Mini / MacBook hostname convention in `scripts/comics-server.sh`)
and health-gates it before every call, so no per-shell setup is needed. On an
unrecognised machine, export `COMICS_SERVER_URL` explicitly.""")

# ---------------------------------------------------------------- wishlist-sellers.md
E("M5", ".claude/commands/comic/wishlist-sellers.md",
r"""**`COMICS_SERVER_URL` must be set.** The script fetches your wish list over HTTP and hard-fails if the server is unreachable. Set it once in `~/.zshrc`:

```bash
# MacBook (connects to Mac Mini over Tailscale)
export COMICS_SERVER_URL=http://mac-mini.tail9b7fa5.ts.net:8080

# Mac Mini (running locally)
export COMICS_SERVER_URL=http://localhost:8080
```

`GIXEN_SERVER_URL` is a deprecated alias — it still works but emits a warning. Migrate to `COMICS_SERVER_URL`.""",
r"""**Resolve the comics server first.** The script fetches your wish list over HTTP from `COMICS_SERVER_URL` and hard-fails if it is unset or unreachable. It is a child process, so the variable must be exported into this shell — use the shared resolver (BUI-172), which honours a preset value or infers it from the Mac Mini / MacBook hostname:

```bash
source "$(git rev-parse --show-toplevel)/scripts/comics-server.sh"
comics_resolve_server || exit 1   # exports COMICS_SERVER_URL for wishlist-sellers below
```""")

# ---------------------------------------------------------------- seller-scan.md
E("M2", ".claude/commands/comic/seller-scan.md",
r"""```bash
cd ~/Projects/comic-pipeline/apps/ebay && \
  .venv/bin/python src/seller_scan.py <seller-username-or-url>
```

If the venv doesn't exist yet:
```bash
cd ~/Projects/comic-pipeline/apps/ebay && python3 -m venv .venv && .venv/bin/pip install -e . -q
```

For JSON output (useful for piping to `/comic:buy`):
```bash
cd ~/Projects/comic-pipeline/apps/ebay && \
  .venv/bin/python src/seller_scan.py <seller> --json
```""",
r"""```bash
seller-scan <seller-username-or-url>
```

`seller-scan` is the installed console script (run `./scripts/install.sh` if it
is not on PATH). For JSON output (useful for piping to `/comic:buy`):
```bash
seller-scan <seller> --json
```""")

E("M2", ".claude/commands/comic/seller-scan.md",
r"""```bash
cd ~/Projects/comic-pipeline/apps/ebay && \
  .venv/bin/python src/seller_scan.py <seller1> <seller2> <seller3> --json
```""",
r"""```bash
seller-scan <seller1> <seller2> <seller3> --json
```""")

E("M18", ".claude/commands/comic/seller-scan.md",
r"""pass **every** seller as a positional arg to **one** `seller_scan.py` invocation in a **single Bash tool call** — do **not** spawn a `Task`/`Agent` subagent per seller.""",
r"""pass **every** seller as a positional arg to **one** `seller-scan` invocation in a **single Bash tool call** — do **not** spawn an `Agent` subagent per seller.""")

E("M2", ".claude/commands/comic/seller-scan.md",
r"""```bash
cd ~/Projects/comic-pipeline/apps/ebay && \
  .venv/bin/python src/seller_scan.py beatlebluecat blissard comichunterlv \
    comics4less davesvintagecomics hodagent ka-761233 punkscrapscomics \
    tunerscomics --json
```""",
r"""```bash
seller-scan beatlebluecat blissard comichunterlv comics4less davesvintagecomics \
  hodagent ka-761233 punkscrapscomics tunerscomics --json
```""")

E("M2", ".claude/commands/comic/seller-scan.md",
r"""```bash
.venv/bin/python src/seller_scan.py <seller>                # only new matches
.venv/bin/python src/seller_scan.py <seller> --show-seen    # every match, cache still active
.venv/bin/python src/seller_scan.py <seller> --no-reject-cache  # only new matches, force re-verify
.venv/bin/python src/seller_scan.py <seller> --all          # every match, force re-verify
.venv/bin/python src/seller_scan.py <seller> --forget       # clear this seller's seen-set, then scan
```""",
r"""```bash
seller-scan <seller>                    # only new matches
seller-scan <seller> --show-seen        # every match, cache still active
seller-scan <seller> --no-reject-cache  # only new matches, force re-verify
seller-scan <seller> --all              # every match, force re-verify
seller-scan <seller> --forget           # clear this seller's seen-set, then scan
```""")

E("M2", ".claude/commands/comic/seller-scan.md",
r"""```bash
seller_scan.py <seller> 2>/dev/null
```""",
r"""```bash
seller-scan <seller> 2>/dev/null
```""")

E("M18", ".claude/commands/comic/seller-scan.md",
r"""Always `0` when `--no-reject-cache` or `--all` is passed (either bypasses the cache; BUI-542 split `--all` into `--show-seen` + `--no-reject-cache` — `--show-seen` alone does NOT zero this out).""",
r"""Always `0` when `--no-reject-cache` or `--all` is passed (either bypasses the cache; `--show-seen` alone does NOT zero this out).""")

E("M2", ".claude/commands/comic/seller-scan.md",
r"""**`seller_scan.py` already guards the seller-scan → `/comic:buy` seam itself""",
r"""**`seller-scan` already guards the seller-scan → `/comic:buy` seam itself""")

if INCLUDE_BUI569:
    E("M1", ".claude/commands/comic/seller-scan.md",
r"""**If this skill is running as a dispatched sub-agent** (rather than inline in
the caller's own context), your final act is to send this output — the
human-readable table or the `--json` object — to the dispatching agent via
`SendMessage`. A sub-agent's plain-text return does not reach the caller on
its own (BUI-569).

""",
r"""""")

# ---------------------------------------------------------------- ezship-add.md
E("M18", ".claude/commands/comic/ezship-add.md",
r"""(the CLI now exits non-zero on a `result: false` rejection)""",
r"""(the CLI exits non-zero on a `result: false` rejection)""")

# ---------------------------------------------------------------- seller_scan.py (M3)
E("M3", "apps/ebay/src/seller_scan.py",
r'''def _verify_via_claude_cli(prompt: str) -> str:
    """Run the verify prompt through the `claude` CLI (subscription auth, no
    ANTHROPIC_API_KEY needed — BUI-270). The prompt goes via stdin (not argv)
    to avoid ARG_MAX/escaping issues on a chunk of candidates.

    Raises _VerifyTimeout on a timeout and RuntimeError on any other transport
    failure (nonzero exit, exec error, or empty stdout) so the caller can fold
    it into the fail-closed chunk-drop path — a CLI hiccup must never leak an
    unverified match — while bisecting only on the timeout.
    """
    try:
        result = subprocess.run(
            ["claude", "-p", "--model", "claude-haiku-4-5-20251001",
             "--output-format", "text"],
            input=prompt, capture_output=True, text=True, timeout=180,
        )
    except subprocess.TimeoutExpired as exc:
        raise _VerifyTimeout(f"claude CLI timed out: {exc}") from exc
    except OSError as exc:
        raise RuntimeError(f"claude CLI failed to start: {exc}") from exc
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "claude CLI failed")
    if not result.stdout.strip():
        raise RuntimeError("claude CLI returned empty output")
    return result.stdout
''',
r'''# JSON Schema the verifier's reply must satisfy. Passed to `claude -p
# --json-schema`, so the CLI validates the model's output and hands back a
# parsed object in the envelope's `structured_output` field — no prose to
# regex a JSON array out of, no "respond with ONLY JSON" prompt scaffold.
_VERIFY_JSON_SCHEMA = json.dumps({
    "type": "object",
    "properties": {
        "rejected": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "integer"},
                    "reason": {"type": "string"},
                },
                "required": ["id", "reason"],
            },
        }
    },
    "required": ["rejected"],
})


def _verify_via_claude_cli(prompt: str) -> str:
    """Run the verify prompt through the `claude` CLI (subscription auth, no
    ANTHROPIC_API_KEY needed — BUI-270). The prompt goes via stdin (not argv)
    to avoid ARG_MAX/escaping issues on a chunk of candidates.

    Uses `--output-format json --json-schema` so the CLI returns a validated
    `structured_output` object; this function returns that object's
    `rejected` array re-serialised as JSON text (the contract the parser and
    every test fake already speak).

    Raises _VerifyTimeout on a timeout and RuntimeError on any other transport
    failure (nonzero exit, exec error, empty stdout, or an envelope with no
    structured output) so the caller can fold it into the fail-closed
    chunk-drop path — a CLI hiccup must never leak an unverified match —
    while bisecting only on the timeout.
    """
    try:
        result = subprocess.run(
            ["claude", "-p", "--model", "claude-haiku-4-5-20251001",
             "--output-format", "json", "--json-schema", _VERIFY_JSON_SCHEMA],
            input=prompt, capture_output=True, text=True, timeout=180,
        )
    except subprocess.TimeoutExpired as exc:
        raise _VerifyTimeout(f"claude CLI timed out: {exc}") from exc
    except OSError as exc:
        raise RuntimeError(f"claude CLI failed to start: {exc}") from exc
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "claude CLI failed")
    if not result.stdout.strip():
        raise RuntimeError("claude CLI returned empty output")
    try:
        envelope = json.loads(result.stdout)
        rejected = envelope["structured_output"]["rejected"]
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise RuntimeError(
            f"claude CLI returned no structured output: {exc}"
        ) from exc
    if not isinstance(rejected, list):
        raise RuntimeError("claude CLI structured output is not a list")
    return json.dumps(rejected)
''')

E("M3", "apps/ebay/src/seller_scan.py",
r'''Respond with a JSON array containing ONLY the ids you are REJECTING, each with a brief reason:
[{{"id": 3, "reason": "X-Factor not X-Men"}}, {{"id": 7, "reason": "annual vs regular"}}]

If nothing is rejected, return [].

Any candidate id NOT present in your response is treated as genuine.''',
r'''List the ids you are REJECTING, each with a brief reason (e.g. "X-Factor not X-Men", "annual vs regular"). Any candidate id you do not reject is treated as genuine.''')

E("M3", "apps/ebay/src/seller_scan.py",
r'''    Returns None if the response could not be parsed/validated.  None means
    "drop this chunk" (fail-closed): catches json.JSONDecodeError plus
    KeyError/ValueError/TypeError during id validation, emits the warning
    messages, and prints the BUI-149 rejected-candidate stderr listing.
    """
    json_match = re.search(r"\[.*\]", text, re.DOTALL)
    if not json_match:
        print(
            f"Warning: could not parse Claude response for candidates "
            f"{chunk_label}; dropping chunk (fail-closed)",
            file=sys.stderr,
        )
        return None

    try:
        rejected_list = json.loads(json_match.group())
    except json.JSONDecodeError:
        print(
            f"Warning: invalid JSON in Claude response for candidates "
            f"{chunk_label}; dropping chunk (fail-closed)",
            file=sys.stderr,
        )
        return None
''',
r'''    Returns None if the response could not be parsed/validated.  None means
    "drop this chunk" (fail-closed): catches json.JSONDecodeError plus
    KeyError/ValueError/TypeError during id validation, emits the warning
    messages, and prints the BUI-149 rejected-candidate stderr listing.

    `text` is the JSON-serialised `rejected` array `_verify_via_claude_cli`
    returns (schema-validated by the CLI), so it is parsed directly — there is
    no prose to scan for a bracketed array.
    """
    try:
        rejected_list = json.loads(text)
    except json.JSONDecodeError:
        print(
            f"Warning: invalid JSON in Claude response for candidates "
            f"{chunk_label}; dropping chunk (fail-closed)",
            file=sys.stderr,
        )
        return None
    if not isinstance(rejected_list, list):
        print(
            f"Warning: could not parse Claude response for candidates "
            f"{chunk_label}; dropping chunk (fail-closed)",
            file=sys.stderr,
        )
        return None
''')

E("M3", "apps/ebay/tests/test_seller_scan.py",
r'''    def test_success_returns_stdout(self, monkeypatch):
        def fake_run(cmd, input, capture_output, text, timeout):
            assert cmd[0] == "claude"
            assert "--model" in cmd
            assert "claude-haiku-4-5-20251001" in cmd
            assert input == "some prompt"
            return subprocess.CompletedProcess(cmd, 0, stdout="[]\n", stderr="")

        monkeypatch.setattr(seller_scan.subprocess, "run", fake_run)

        result = seller_scan._verify_via_claude_cli("some prompt")

        assert result == "[]\n"
''',
r'''    def test_success_returns_rejected_array_from_structured_output(self, monkeypatch):
        envelope = json.dumps({
            "type": "result",
            "result": "{\"rejected\":[{\"id\":2,\"reason\":\"annual vs regular\"}]}",
            "structured_output": {"rejected": [{"id": 2, "reason": "annual vs regular"}]},
        })

        def fake_run(cmd, input, capture_output, text, timeout):
            assert cmd[0] == "claude"
            assert "--model" in cmd
            assert "claude-haiku-4-5-20251001" in cmd
            assert cmd[cmd.index("--output-format") + 1] == "json"
            assert "--json-schema" in cmd
            assert input == "some prompt"
            return subprocess.CompletedProcess(cmd, 0, stdout=envelope + "\n", stderr="")

        monkeypatch.setattr(seller_scan.subprocess, "run", fake_run)

        result = seller_scan._verify_via_claude_cli("some prompt")

        assert json.loads(result) == [{"id": 2, "reason": "annual vs regular"}]

    def test_envelope_without_structured_output_raises_runtime_error(self, monkeypatch):
        """A schema-validation miss or an error envelope has no
        `structured_output`; that is a transport failure (fail-closed chunk
        drop), never an empty rejection list."""
        def fake_run(cmd, input, capture_output, text, timeout):
            return subprocess.CompletedProcess(
                cmd, 0, stdout=json.dumps({"type": "result", "result": "[]"}), stderr=""
            )

        monkeypatch.setattr(seller_scan.subprocess, "run", fake_run)

        with pytest.raises(RuntimeError, match="no structured output"):
            seller_scan._verify_via_claude_cli("some prompt")
''')


E("M3", "apps/ebay/tests/test_seller_scan.py",
r"""        bullets_a = prompt_a.split("Reject if:")[1].split("Respond with")[0]
        bullets_b = prompt_b.split("Reject if:")[1].split("Respond with")[0]""",
r"""        bullets_a = prompt_a.split("Reject if:")[1].split("List the ids")[0]
        bullets_b = prompt_b.split("Reject if:")[1].split("List the ids")[0]""")


# ---------------------------------------------------------------- driver
def main() -> int:
    if NEW.exists():
        shutil.rmtree(NEW)
    files: dict[str, str] = {}
    for finding, rel, old, new, count in EDITS:
        if rel not in files:
            files[rel] = (REPO / rel).read_text()
        s = files[rel]
        n = s.count(old)
        if n != count:
            print(f"FAIL [{finding}] {rel}: expected {count} match(es), found {n}\n--- old ---\n{old[:300]}", file=sys.stderr)
            return 1
        files[rel] = s.replace(old, new)
    chunks = []
    for rel, content in files.items():
        out = NEW / rel
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(content)
        r = subprocess.run(
            ["diff", "-u", "--label", f"a/{rel}", "--label", f"b/{rel}", str(REPO / rel), str(out)],
            capture_output=True, text=True,
        )
        if r.returncode == 1:
            chunks.append(r.stdout)
        elif r.returncode != 0:
            print(r.stderr, file=sys.stderr)
            return 1
    PATCH_OUT.write_text("".join(chunks))
    chk = subprocess.run(["git", "-C", str(REPO), "apply", "--check", str(PATCH_OUT)], capture_output=True, text=True)
    print(f"edits: {len(EDITS)}  files: {len(files)}  patch: {PATCH_OUT}")
    print("git apply --check:", "OK" if chk.returncode == 0 else chk.stderr)
    stat = subprocess.run(["git", "-C", str(REPO), "apply", "--stat", str(PATCH_OUT)], capture_output=True, text=True)
    print(stat.stdout)
    return chk.returncode


if __name__ == "__main__":
    sys.exit(main())
