---
name: comic:collection-add
description: Add won Gixen auctions to the LOCG collection end-to-end. Pulls won snipes, resolves LOCG IDs, dedupes against existing lists, adds with price+grade defaults, and cleans up the wish-list. No clarifying questions.
---

# Comic Collection Add

Drive the full add-to-LOCG flow from a single invocation. Source is Gixen won auctions. Defaults are fixed (see Hard Rules). The only points this skill asks the user are variant disambiguation and when LOCG ID resolution fails entirely.

**Gixen CLI:** `cd ~/Projects/gixen-cli && .venv/bin/python cli.py`
**LOCG CLI:** `cd ~/Projects/locg-cli && PYTHONPATH=src python3 -m locg` (lookup/search only — not for adds, checks, or removes)

## Hard Rules

- No clarifying questions before step 1. Source is Gixen, defaults are fixed.
- Price = `current_bid` from gixen, parsed as float (strip "USD" and whitespace).
- Grade = `comic_grade` from gixen if present. Omit entirely if missing — never guess, never default to 9.4.
- **All LOCG collection operations (check, add, remove) use Playwright** — not the LOCG CLI. The LOCG CLI is only used for `locg lookup` (ID resolution). See Playwright Setup.
- After any failed add (non-200), verify actual collection state from the page HTML before treating as failure. See Step 5.
- On JSON parse error from any call, retry once after 2s. After 2 failures, log and continue.

## Playwright Setup

LOCG is protected by Cloudflare's JS challenge. The LOCG CLI's HTTP client (`curl_cffi`) is blocked for all mutation operations. Use Playwright with real Chrome for all collection checks, adds, and removes.

Write and run a Python script using this pattern:

```python
from playwright.sync_api import sync_playwright
import os, re, time

LOCG_USER = os.environ["LOCG_USERNAME"]
LOCG_PASS = os.environ["LOCG_PASSWORD"]
BASE = "https://leagueofcomicgeeks.com"

def api_post(context, path, data):
    resp = context.request.post(
        f"{BASE}{path}", form=data,
        headers={"X-Requested-With": "XMLHttpRequest"}
    )
    return resp.status, resp.text()

def check_page_lists(html):
    controllers = re.findall(
        r'class="([^"]*comic-controller[^"]*)"[^>]*data-list="(\d+)"', html)
    in_collection = any("active" in c and lid == "2" for c, lid in controllers)
    in_wish = any("active" in c and lid == "3" for c, lid in controllers)
    return in_collection, in_wish

with sync_playwright() as p:
    browser = p.chromium.launch(
        channel='chrome', headless=False,
        args=['--disable-blink-features=AutomationControlled'])
    context = browser.new_context()
    page = context.new_page()

    # Login
    page.goto(f"{BASE}/login", wait_until='domcontentloaded')
    page.wait_for_selector('input[name="username"]', timeout=20000)
    page.fill('input[name="username"]', LOCG_USER)
    page.fill('input[name="password"]', LOCG_PASS)
    page.keyboard.press('Enter')
    page.wait_for_url('**/dashboard**', timeout=15000)

    # Steps 4–6 run inside this block
```

Key rules:
- Always `headless=False` — Cloudflare blocks headless Chrome.
- Always `wait_until='domcontentloaded'` for navigations — `networkidle` times out during CF challenges.
- Use `context.request.post()` for API calls — not `page.evaluate()` fetch (which CF challenges separately).

## 1. Source: Won Auctions

```bash
cd ~/Projects/gixen-cli && .venv/bin/python cli.py list --json 2>/dev/null
```

Filter to wins:
- `time_to_end == "ENDED"`
- `status` matches `WON` (case-insensitive substring; e.g. `"WON"`, `"YOU WON"`)

If no wins, print "No won auctions to add." and stop.

## 2. Resolve LOCG IDs

For each won snipe, resolve a LOCG comic ID for **every issue** in the lot.

**Preferred — use the snipe's `comics` array.** Each snipe now carries a `comics: [{issue, locg_id, locg_variant_id, is_primary}, ...]` array on the JSON list output. Iterate it; if a row already has `locg_id` populated, reuse it as-is. Single-issue wins have a one-element array; lots have one entry per issue.

**Fallback — `locg lookup` batch.** For any comic still missing a `locg_id`, batch them in a single call:

```bash
cd ~/Projects/locg-cli && PYTHONPATH=src python3 -m locg lookup \
  "Daredevil:2" "Daredevil:3" "Daredevil:4" \
  --no-collection --pretty 2>/dev/null
```

`locg lookup` groups by series (one search per unique series), uses a title-filtered query per issue (no 140-issue page limit), and caches resolved IDs to disk so repeats across runs are free.

Series name + issue numbers come from the snipe record. Issue numbers come from each entry in the `comics` array. Lots whose `comics` array only has the primary issue (parser missed the lot expansion) need fallback lookup for the missing issues — extract them from the snipe's `title` (e.g. "Daredevil 1,2,3,4,5").

**If lookup returns zero results:** Do not silently skip. Print the comic title and ask the user for the LOCG URL. Accept a URL in the form `https://leagueofcomicgeeks.com/comic/{id}/...?variant={variant_id}` and extract the numeric `id` (and `variant_id` if present).

## 3. Variant Disambiguation

When fallback lookup returns multiple candidates, pick in this order:

1. If gixen `fmv_notes` mentions "Newsstand" → prefer the Newsstand variant.
2. If gixen `title` implies a specific cover (e.g. "Spider-Man Homage Cover" → match `"homage"` in candidate name), prefer that.
3. Otherwise prefer the regular cover (no variant suffix in title).
4. Still ambiguous → list candidates and ask the user. **Only at this point — not earlier.**

## 4. Dedupe (Playwright)

Navigate to each comic's page and read collection state from the HTML:

```python
page.goto(f"{BASE}/comic/{comic_id}/x", wait_until='domcontentloaded')
html = page.content()
in_collection, in_wish = check_page_lists(html)
```

For each ID:
- `in_collection` → skip, mark as duplicate in the report.
- `in_wish` → still add to collection, queue for wish-list removal in step 6.
- Neither → add normally.

This navigation also warms the CF session for the subsequent POST in step 5.

## 5. Add to Collection (Playwright)

For each ID to add:

```python
# Navigate to warm CF session (reuse page already loaded in step 4 if sequential)
page.goto(f"{BASE}/comic/{comic_id}/x", wait_until='domcontentloaded')
time.sleep(1)

# Add to collection (list_id=2, action_id=1)
status, body = api_post(context, '/comic/my_list_move', {
    'comic_id': str(comic_id), 'list_id': '2', 'action_id': '1'
})

# Verify actual state — 403 may be a phantom add (server committed, CF blocked client response)
page.reload(wait_until='domcontentloaded')
html = page.content()
actually_in_collection, _ = check_page_lists(html)

if status == 200 or actually_in_collection:
    # Save grade and price — always run regardless of add response code
    time.sleep(1)
    details = {
        'comic_id': str(comic_id),
        'price_paid': str(price),
        'grading': str(grade) if grade else '',
        'copy_num': '', 'quantity': '', 'date_purchased': '',
        'purchase_store': '', 'media': '', 'signature': '',
        'storage_box': '', 'slabbing': '', 'grading_company': '',
        'condition': '', 'notes': '', 'owner': '',
    }
    api_post(context, '/comic/post_my_details', details)
else:
    log_failure(comic_id, f"add failed (status {status}, not in collection after verify)")
```

Grade rule: include `grading` only if `comic_grade` is present in the snipe record. Pass empty string otherwise — never omit the key from `post_my_details` entirely, as missing fields get wiped to server defaults.

## 5b. Persist LOCG IDs back to Gixen

Right after a confirmed successful add, write the resolved LOCG ID back to Gixen:

```bash
cd ~/Projects/gixen-cli && .venv/bin/python cli.py locg link <item_id> <locg_id> [--issue N] [--variant-id V] 2>/dev/null
```

- For **single-issue wins**: omit `--issue`.
- For **lots**: call once per issue with `--issue N`.
- Pass `--variant-id` only when a distinct variant ID exists.
- On non-zero exit, log it but do NOT abort — write-back is best-effort. The LOCG state is correct from step 5; the Gixen cache just won't be primed for next time.

## 6. Wish-list Cleanup (Playwright)

For every ID queued in step 4 (was in `wish`) and confirmed added in step 5:

```python
time.sleep(1)
api_post(context, '/comic/my_list_move', {
    'comic_id': str(comic_id), 'list_id': '3', 'action_id': '0'
})
```

## 7. Final Report

Print a markdown table of what was added, plus a Total row:

```
**Added (N):**

| Comic | LOCG ID | Grade | Price |
|---|---|---|---|
| Amazing Spider-Man #300 | 9559460 | 9.2 | $440.00 |
| Daredevil #29 | 8823401 | — | $16.28 |
| **Total** | | | **$456.28** |
```

Use `—` for missing grade. Below the table, list:

- **Skipped (already in collection):** IDs and titles, if any.
- **Wish-list cleaned:** IDs removed from wish, if any.
- **Failed:** IDs and error messages, if any (after retry).
- **Needs manual lookup:** Comics where LOCG ID could not be resolved and user did not provide a URL.
