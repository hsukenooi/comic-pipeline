# PER-34 Manual E2E Test Checklist — /comic:buy Flow

Run the static checks first (`pytest tests/test_skill_migration.py`). Only proceed if all pass.

## Pre-Flight

| Check | Pass? |
|---|---|
| `GIXEN_SERVER_URL` is set and `curl -sf $GIXEN_SERVER_URL/health` returns 200 | |
| `~/.config/ebay-fetch/config.json` exists with `client_id` and `client_secret` keys | |
| `~/.config/gixen-server/.env` has `GIXEN_USERNAME` and `GIXEN_PASSWORD` | |
| LOCG session active (collection-check/add requires browser login) | |
| At least one eBay auction URL available for testing | |

## Step 1: identify

In a Claude Code session, run `/comic:identify` with a test eBay listing URL.

**What to verify:**
- `cd ~/Projects/comic-pipeline/apps/ebay && python src/ebay_fetch.py --json <item_id>` returns valid JSON
- Skill produces identification table with title, issue, grade, variant, listing type columns
- No path errors or "file not found" messages

| Result | Pass? |
|---|---|
| ebay_fetch.py executes without error | |
| JSON response contains `title`, `listing_type`, `grade` fields | |
| Identification table rendered to user | |

## Step 2: collection-check

After identify, run `/comic:collection-check` (or continue with `/comic:buy` which orchestrates it).

**What to verify:**
- LOCG lookup runs against the identified comic
- `in_collection` flag is set correctly
- `locg_id` is resolved if the comic exists in LOCG

| Result | Pass? |
|---|---|
| LOCG lookup completes | |
| `in_collection` flag accurate | |
| `locg_id` populated for known comics | |

## Step 2.5: grade (conditional)

Only runs if `grade_source` is `"missing"` AND no grade signal in title.

**What to verify:**
- `~/Projects/comic-pipeline/apps/ebay/src/ebay_fetch.py` fetches images (via Browse API)
- Images download to `/tmp/comic-grading/`
- 3 grader agents dispatch and return consensus grade

| Result | Pass? |
|---|---|
| Images download to correct path | |
| Grader agents return output | |
| Consensus grade produced | |

## Step 3: fmv

Run `gixen-cli fmv` (via buy.md Step 3 or directly).

**What to verify:**
- `cd ~/Projects/gixen-cli && .venv/bin/python cli.py fmv --batch <json>` runs
- FMV table with range, confidence, n, and CV appears
- DB upsert completes (returns comic `id`)

| Result | Pass? |
|---|---|
| FMV CLI runs without error | |
| FMV table shows plausible price range | |
| `POST $GIXEN_SERVER_URL/api/comics` upsert succeeds | |

## Step 4: snipe-add

Approve a max bid and run `/comic:snipe-add`.

**What to verify:**
- `gixen-cli add <item_id> <max_bid> --comic "..." --locg-id <id>` succeeds
- Gixen confirmation appears (item shows in `gixen-cli list`)
- Comic linked to snipe in DB (`GET $GIXEN_SERVER_URL/api/snipes` shows `comic_id`)

| Result | Pass? |
|---|---|
| `gixen-cli add` executes without session error | |
| Item appears in `gixen-cli list` output | |
| Snipe record has `comic_id` set | |

## Step 5: ezship-add (post-win, optional)

After winning a snipe, run `/comic:ezship-add` to verify the new path.

**What to verify:**
- `cd ~/Projects/comic-pipeline/apps/ezship && npx tsx src/cli.ts new -t <tracking> -c <carrier>` runs
- Session cookie at `~/.config/ezship/config.json` is valid

| Result | Pass? |
|---|---|
| ezship CLI runs without error | |
| Order submitted (or session expired message if cookie stale) | |

## Summary

| Step | Result |
|---|---|
| Pre-flight | |
| identify | |
| collection-check | |
| grade (if triggered) | |
| fmv | |
| snipe-add | |
| ezship-add (optional) | |

**Tester:** ___  **Date:** ___  **Overall:** Pass / Fail
