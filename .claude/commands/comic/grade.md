---
name: comic:grade
description: Grade the physical condition of raw comics from eBay listing photos using value-gated independent sub-agents (1 grader for cheap/unambiguous lots, escalating to a 3-grader panel for high-value or boundary-ambiguous ones) and CGC/Overstreet criteria. Use when the user wants a condition assessment before bidding or evaluating a listing. Produces CGC-scale numeric grades with a confidence level, grade range, and defect breakdowns.
---

# Comic Grade

Grade raw (ungraded) comics from eBay seller photos. A first grader runs on every comic; high-value or boundary-ambiguous comics escalate to a 3-grader panel whose independent grades are synthesized into a consensus. Each grade carries a coverage-driven confidence level and grade range. Outputs match `/comic:fmv` input format.

## Input

One or more eBay listing URLs or item IDs. No seller-stated grade needed — this skill derives it from photos.

## Step 1: Download Listing Photos

Use the eBay Browse API via `~/Projects/comic-pipeline/apps/ebay/src/ebay_fetch.py` — the `get_item_by_legacy_id` endpoint returns `image` and `additionalImages` with direct `i.ebayimg.com` URLs that are downloadable without bot detection. Do not scrape eBay HTML pages (returns 400/CAPTCHA).

```python
#!/usr/bin/env python3
import base64, json, os, urllib.request, requests
from pathlib import Path

with open(Path("~/.config/ebay-fetch/config.json").expanduser()) as _f:
    _cfg = json.load(_f)
APP_ID = os.environ.get("EBAY_CLIENT_ID") or _cfg.get("client_id")
CERT_ID = os.environ.get("EBAY_CLIENT_SECRET") or _cfg.get("client_secret")
BASE_URL = "https://api.ebay.com"

def get_token():
    creds = base64.b64encode(f"{APP_ID}:{CERT_ID}".encode()).decode()
    return requests.post(
        f"{BASE_URL}/identity/v1/oauth2/token",
        headers={"Authorization": f"Basic {creds}", "Content-Type": "application/x-www-form-urlencoded"},
        data={"grant_type": "client_credentials", "scope": "https://api.ebay.com/oauth/api_scope"},
        timeout=10,
    ).json()["access_token"]

def download_listing(token, item_id, outdir):
    outdir = Path(outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    headers = {"Authorization": f"Bearer {token}", "X-EBAY-C-MARKETPLACE-ID": "EBAY_US"}
    data = requests.get(
        f"{BASE_URL}/buy/browse/v1/item/get_item_by_legacy_id",
        headers=headers, params={"legacy_item_id": item_id}, timeout=10,
    ).json()
    imgs = []
    if "image" in data:
        imgs.append(data["image"]["imageUrl"])
    for ai in data.get("additionalImages", []):
        imgs.append(ai["imageUrl"])
    for i, url in enumerate(imgs, 1):
        urllib.request.urlretrieve(url, outdir / f"img-{i:02d}.jpg")
    # Auction value signal for the value gate (Step 2): currentBidPrice/bidCount are
    # already in the Browse API response — capture them, no extra request needed.
    price_node = data.get("currentBidPrice") or data.get("price") or {}
    try:
        current_price = float(price_node.get("value")) if price_node.get("value") is not None else None
    except (TypeError, ValueError):
        current_price = None
    return {
        "title": data.get("title", item_id),
        "image_count": len(imgs),
        "current_price": current_price,            # USD float, or None for BIN/unknown
        "bid_count": data.get("bidCount"),         # int for auctions, None otherwise
    }

WORKDIR = "/tmp/comic-grading"
items = [("comic-1", "178057470740"), ("comic-2", "178057488707"), ...]
token = get_token()
for label, item_id in items:
    result = download_listing(token, item_id, f"{WORKDIR}/{label}")
    price = result["current_price"]
    price_str = f"${price:.2f}" if price is not None else "n/a"
    print(f"{label}: {result['title']} — {result['image_count']} images — current bid {price_str} ({result['bid_count']} bids)")
```

The printed `current_price` per comic is the **value signal for the Step 2 value gate** — capture it alongside the item id (e.g. keep the `result` dicts) so escalation can branch on auction value. `current_price` is `None` for fixed-price listings or when the field is absent; treat unknown value as "below threshold" (single grader) unless other signals say otherwise.

Output directory layout:
```
/tmp/comic-grading/
  comic-1/
    img-01.jpg   ← front cover (first image returned by API)
    img-02.jpg   ← additional images
    ...
```

No `listing.html` is produced. In the grader prompt, note "no seller description available" unless the seller's grade is known from the listing title (retrieved by `ebay_fetch.py`).

## Step 2: Dispatch Grader Agents (value-gated)

Don't fan out 3 graders for every comic — most listings in a seller scan are cheap, and 3-per-comic burns agents where 1 will do. **Run 1 grader first, then escalate to a 3-grader panel only when the comic earns it.** Use the `general-purpose` subagent type; the grader prompt template below is identical whether you run 1 or 3.

**Tunable gate constants** (stated here so they're easy to adjust):
- `VALUE_THRESHOLD = $25` — `current_price` (from Step 1) at or above this always gets the full 3-grader panel; an expensive book justifies the rigor.
- `CAP_BAND = 0.5` — if the single grader's grade sits within this many points of a grade-capping threshold (the spine-split / missing-piece / detached-cover ceilings), treat it as boundary-ambiguous.

**Escalate the single-grader result to a full 3-grader panel when ANY of these hold:**
1. **Value:** `current_price ≥ VALUE_THRESHOLD` (or the listing is a known key regardless of current bid).
2. **Boundary-ambiguous grade:** the grade is within `CAP_BAND` of a grade-capping threshold, OR the grader flagged the grade as uncertain / gave a wide GRADE RANGE (≥1.5 pts), OR a possible-restoration flag fired.
3. **Decision-relevant:** a half-grade swing would plausibly cross the buy/no-buy line the user cares about (if known at grade time).

**Stay at the single grader when** the auction is below `VALUE_THRESHOLD`, the grade is unambiguous, and no cap/restoration flag fired. Unknown `current_price` counts as below-threshold.

**Dispatch mechanics:**
- Run the **first** grader for every comic in one parallel batch (N comics = N parallel calls).
- Then, for the comics that tripped a gate, dispatch the **remaining 2** graders — again all in one parallel batch. (Two batches total, not one-at-a-time.)
- The grader prompt and criteria are identical across batches so the 3 panel grades stay independent and comparable.

**Required per-comic reporting (no silent caps):** for every comic, state how many graders ran and why — e.g. `1 grader (──$6, unambiguous)` or `3 graders (──$40 ≥ $25 value threshold)` or `3 graders (grade 5.0 within 0.5 of the 1/2" spine-split cap)`. The user must be able to see where rigor was and wasn't spent.

### Grader Prompt Template

Adapt per comic (fill in `{COMIC}`, `{YEAR}`, `{FOLDER}`, `{N}`):

```
You are an expert vintage comic book grader. Grade the physical condition of a raw (ungraded) comic from the seller's eBay photos.

COMIC: {COMIC} ({YEAR})
IMAGE FOLDER: {FOLDER}
IMAGES: img-01.jpg through img-{N:02d}.jpg ({N} photos of the seller's copy)

GRADING SCALE (Heritage/Overstreet — use these numeric values):
9.8 NM/MT | 9.6 NM+ | 9.4 NM | 9.2 NM- | 9.0 VF/NM | 8.5 VF+ | 8.0 VF | 7.5 VF- | 7.0 FN/VF | 6.5 FN+ | 6.0 FN | 5.5 FN- | 5.0 VG/FN | 4.5 VG+ | 4.0 VG | 3.5 VG- | 3.0 GD/VG | 2.5 GD+ | 2.0 GD | 1.8 GD- | 1.5 FR/GD | 1.0 FR | 0.5 PR

DETAILED CRITERIA BY GRADE (Heritage Auctions / Overstreet):

9.4 NM — Cover flat, no surface wear. Inks bright, minimal fading. Corners cut square, ever-so-slight blunting OK. 1/16" bend no color break. Bindery tears <1/16". Spine tight and flat, almost no stress lines. Staples generally centered, slight discoloration OK. Paper off-white to cream, supple. Slight interior tears OK.

9.0 VF/NM — Almost flat with almost imperceptible wear. Inks bright, slightly diminished reflectivity. 1/8" bend if color not broken. Corners square, ever-so-slight blunting, no creases. Spine tight and flat. Slightest staple tears OK. Very minor accumulation of stress lines if nearly imperceptible. Paper off-white to cream, supple.

8.0 VF — Excellent copy, outstanding eye appeal. Inks generally bright, moderate to high reflectivity. 1/4" crease OK if color not broken. Spine almost completely flat, possible minor color break. Very slight staple tears, few almost insignificant stress lines. Paper cream to tan, supple. Centerfold mostly secure. Minor interior tears at margin OK.

7.0 FN/VF — Minor wear, still relatively flat and clean. Inks generally bright, moderate reduction in reflectivity. Corners may be blunted. Slightest spine roll, possible moderate color break. Slight staple tears, small accumulation of light stress lines. Slight rust migration. Paper cream to tan. Centerfold mostly secure.

6.0 FN — Minor wear, no significant creasing. Inks show significant reduction in reflectivity. Blunted corners more common, minor staining/soiling/foxing OK. Minor spine roll. Up to 1/4" spine split OR severe color break. Minor staple tears, few slight stress lines, minor rust migration. Paper tan to brown, fairly supple, no brittleness. Centerfold may be loose.

5.0 VG/FN — Well used but above average. Inks have moderate to low reflectivity. Minor to moderate creases/dimples. Minor to moderate spine roll. Spine split up to 1/2". Minor staple tears and stress lines, minor rust migration. Paper tan to brown, no brittleness. Centerfold may be loose.

4.0 VG — Average used copy. Cover may be loose but not detached. Reflectivity low. Moderate creases/dimples. Corners may be blunted. Missing piece up to 1/4" triangle or 1/8" square OK. Store stamps, arrival dates, initials have no effect on grade. Minor unobtrusive tape OK on otherwise high-grade copies. Moderate spine roll and/or 1" spine split. Staples may be discolored. Minor to moderate staple tears and stress lines, some rust migration. Paper brown, not brittle. Centerfold may be loose or detached at ONE staple.

3.0 GD/VG — Substantial wear. Cover may be loose or detached at one staple. Reflectivity very low. Book-length crease/dimples OK. Corners may be blunted or rounded. Missing piece 1/4"–1/2" triangle or 1/8"–1/4" square OK. Tape OK. Moderate spine roll. Spine split 1"–1.5". Staples may be rusted or replaced. Paper brown, not brittle. Centerfold may be loose or detached at one staple.

2.0 GD — Reading copy. Cover may be detached. Reflectivity low to absent. Book-length creases/dimples. Rounded corners more common. Missing piece up to 1/2" triangle or 1/4" square from front or back (not both). Tape common. Spine roll likely. Spine split up to 2". Staples may be degraded/replaced/missing. Paper brown, not brittle. Centerfold may be loose or detached.

1.5 FR/GD — Creased, scuffed, abraded, soiled. Cover may be detached. Almost no reflectivity. Up to 1/10 of back cover may be missing. Spine split 2"–2/3 book length. Paper brown, may show brittleness at edges.

1.0 FR — Heavy wear. Up to 1/4 of front cover missing OR no back cover (not both). Spine split up to 2/3 book length. Paper brown, brittleness at edges but not central pages.

0.5 PR — Brittle, often incomplete. Covers may be detached with large chunks missing. Complete book-length spine split possible. Paper brittle throughout.

KEY GRADING SIGNALS — USE THESE TO ANCHOR YOUR GRADE:

INK REFLECTIVITY — USE AS A CONFIRMING SIGNAL, NOT A LEAD:
eBay photos are often taken under direct overhead lighting, which washes out reflectivity on high-grade copies and makes mid-grade copies look better than they are. Do NOT lead with reflectivity. Let physical defects (creases, spine splits, corner wear) anchor the grade first, then use reflectivity to confirm or adjust by at most one half-grade. If reflectivity conflicts with physical defect evidence, trust the defects.
- Bright, high reflectivity → consistent with NM range (9.x)
- Moderate to high → consistent with VF (8.0)
- Moderate reduction → consistent with FN/VF (7.0)
- Significant reduction → consistent with FN (6.0)
- Moderate to low → consistent with VG/FN (5.0)
- Low → consistent with VG (4.0) / GD (2.0)
- Absent → consistent with FR (1.0)

PAPER COLOR (visible on page edges and interior shots):
- White, supple → NM (9.x)
- Off-white to cream → VF/NM–VF (9.0–8.0)
- Cream to tan → VF–FN/VF (8.0–7.0)
- Tan to brown, supple, no brittleness → FN–VG/FN (6.0–5.0)
- Brown, not brittle → VG–GD (4.0–2.0)
- Brown, brittleness at edges → FR/GD (1.5)
- Brittle throughout → PR (0.5)

SPINE SPLIT SIZE:
- None → VF+ and above
- 1/4" → FN (6.0)
- 1/2" → VG/FN (5.0)
- 1" → VG (4.0)
- 1"–1.5" → GD/VG (3.0)
- 2" → GD (2.0)
- 2"–2/3 book → FR/GD (1.5)
- Full length → PR (0.5)

CENTERFOLD STATUS:
- Secure → VF (8.0) and above
- Mostly secure → VF (8.0) / FN/VF (7.0)
- May be loose → FN (6.0) through VG/FN (5.0)
- Loose or detached at ONE staple → VG (4.0) / GD/VG (3.0)
- Loose or detached → GD (2.0)
- May be missing → FR (1.0)

WHAT TO EXAMINE:
1. FRONT COVER — color fading, dust shadow, soiling, stains, writing (see PRINT-LAYER RULE + WRITING RULE below — printed credits/signatures are NOT writing), fingerprints, tape, creases (measure if possible), surface tears, missing pieces (triangle or square size)
2. SPINE — stress lines (count; note color-breaking vs. impression-only), spine split (measure length), rolling degree
3. CORNERS — all four: blunting, crunches, folds, chips, missing tips
4. EDGES — chipping, tears, foxing, water staining
5. STAPLES — rust, popping, migration to surrounding paper, replacement vs. original
6. BACK COVER — same checks; price box; stamps; soiling; tanning; missing piece size
7. INTERIOR PAGES — paper color (white/off-white/cream/tan/brown), brittleness signs, foxing, missing pieces, centerfold status
8. STRUCTURAL — cover detached? subscription crease? cover roll?

PRINT-LAYER RULE (printed elements are NEVER defects):
Anything reproduced in the original printing is part of the cover art, not damage — printed creator credits, printed/facsimile signatures, barcodes, price boxes, cover text, and logos. Per CGC's defect taxonomy, "Writing" and "Name Written on Cover" are *substance* defects (added to the paper after printing); printed cover elements are not in the defect taxonomy at all. None of them affect the grade — ever. Only marks physically ADDED to the paper AFTER printing (pen, marker, pencil, post-print stamps, stickers) can be defects.

How to tell print-layer from post-print (use this before calling anything a signature):
- Print-layer (NOT a defect): looks identical on every copy; NO paper indentation or pressure groove; ink sits flush with the surface; ink color and 45°-reflection match the surrounding printed text.
- Post-print (a defect): visible pressure groove in the paper; variable ink density; darker where strokes overlap; reflection distinct from the printed ink.

Grade impact:
- Printed or facsimile signature / printed creator credit → ZERO effect on grade. Do NOT cap.
- Authentic post-print autograph (pen/marker added after printing) → a "writing" substance defect; apply the WRITING RULE below.
- If you CANNOT tell from the photos whether a mark is print-layer or post-print → DEFAULT TO PRINT-LAYER (do NOT cap), and flag the uncertainty in PHOTO LIMITATIONS. Capping a real printed credit as if it were a signature is the specific failure this rule exists to prevent — when unsure, do not cap.

WRITING RULE (applies only to AUTHENTIC post-print writing, confirmed via the print-layer test above):
- Writing on story pages (editorial content): major defect — treat as a grade-significant deduction
- Writing on non-story pages (ads, inside front/back cover, indicia page): minor detractor — note it but do not drive the grade down more than 0.5 pts
- If you cannot determine which type of page the writing is on, note it and flag it as uncertain

GRADE-CAPPING DEFECTS:
Some single defects set a hard ceiling regardless of otherwise high condition. Before assigning a final grade, check for these ceilings and state the cap explicitly in your rationale. (Printed cover elements — creator credits, facsimile signatures, barcodes, price boxes — are NEVER grade-capping; see PRINT-LAYER RULE. Only an authentic post-print autograph can act as a writing defect.)
- Spine split 1/4" → caps at FN (6.0)
- Spine split 1/2" → caps at VG/FN (5.0)
- Spine split 1" → caps at VG (4.0)
- Spine split 1"–1.5" → caps at GD/VG (3.0)
- Spine split 2" → caps at GD (2.0)
- Missing piece > 1/2" triangle or > 1/4" square → caps at GD (2.0)
- Cover detached at both staples → caps at GD (2.0)
- More than 1/4 of front cover missing → caps at FR (1.0)

VISUAL RESTORATION RED FLAGS (note in PHOTO LIMITATIONS if observed):
These are signs that a book may have been restored. CGC will designate restored copies and drop the grade significantly. Flag if you see any:
- Suspiciously uniform, even color across the cover with no expected fading gradient
- Spine too tight and crease-free relative to heavy corner wear (suggests spine glue)
- Staples unusually clean/shiny relative to page tanning or cover aging
- Color noticeably brighter in one isolated region (e.g., one corner) than the rest of the cover
- Cover edges too crisp relative to interior page color
If you see 2+ red flags, note "possible restoration — black-light examination needed" in PHOTO LIMITATIONS.

PHOTO COVERAGE & CONFIDENCE — derive confidence from WHICH VIEWS you have, not from how many images:
Grade confidence is capped by coverage, not image count. The defects that separate high grades are invisible without specific views, so a grade from sparse coverage is structurally uncertain no matter how clean the visible surfaces look. Two photos of the front+back cover are far more useful than two photos of the front alone — judge views, not counts.

VIEW → WHAT IT LETS YOU ASSESS (and what you CANNOT confirm without it):
- Front cover (flat, straight-on): surface soiling, stains, tape, larger creases, front-cover fade, corner blunting if resolution allows
- Back cover (flat): back-cover defects; fade is only judgeable by comparing front vs. back color
- Spine straight-on: spine roll, miswrap, spine split, color breaks along the spine
- Spine under RAKING / angled light: non-color-breaking stress lines, finger bends, cockling/canvassing/rippling — ESSENTIALLY INVISIBLE in flat overhead light
- Four corners (close-up): blunting, chips, tears, missing tips
- Staples (close-up): exterior rust, popped or replaced staples
- Interior / centerfold spread: centerfold attachment, staple rust MIGRATION staining, interior tears/writing
- Page edge (close-up): paper tanning/browning, brittleness
Without a raking-light spine shot you CANNOT confirm spine stress lines; without an interior shot you CANNOT confirm centerfold attachment or staple migration; without a page-edge shot you CANNOT confirm paper brittleness/tanning. List each missing-view gap as un-assessed in PHOTO LIMITATIONS.

CONFIDENCE LEVELS (assign exactly one, anchored to coverage):
- HIGH — front + back + spine, plus at least one of {raking-light spine, interior/centerfold, page edge}, all clear and in focus. Enough coverage to see where high-grade defects hide.
- MEDIUM — front + back (or front + spine) clear, but the grade-separating views (raking spine / interior / page edge) are absent.
- MEDIUM-LOW — exactly the cover faces with no spine/interior/edge detail (the common 2-photo qualitycomix case).
- LOW — a single usable view, only the front cover, or blurry/partial photos.
HARD CEILING: with 2 or fewer usable cover views and no spine-raking / interior / page-edge shot, confidence CANNOT exceed MEDIUM-LOW regardless of how clean the book looks — you have not seen the surfaces that separate a 9.x from a 7.x.

GRADE RANGE: when confidence is MEDIUM-LOW or LOW, report a grade RANGE spanning the plausible outcomes given what you cannot see (e.g. "5.0–6.0 VG/FN–FN"), with the single GRADE as your best point estimate inside that range. At HIGH confidence the range may collapse to the point grade.

PROCEDURE:
1. Read listing.html from {FOLDER} and scan the seller's description for any disclosed defects, restoration, or condition notes. Note these before viewing photos — they may reveal things photos don't show.
2. Use the Read tool on every img-XX.jpg in the folder (read all {N}).
3. Before grading, map each photo to its content type: front cover / spine view / back cover / interior pages / detail shot / other. Note the mapping explicitly (e.g., "img-01: front cover, img-02: spine, img-03: back cover").
4. Assess PHOTO COVERAGE: list which views from the table above are present, and set your CONFIDENCE ceiling from coverage before you finalize the grade.
5. Note each defect with its location, referencing the photo where you saw it.
6. Identify any grade-capping defects and state the ceiling explicitly.
7. Apply the CGC scale; anchor on physical defects first, use reflectivity to confirm.
8. Be rigorous — do NOT inflate. Grade only what you can see, and let coverage cap your confidence.

OUTPUT FORMAT (exactly this, no preamble):
PHOTO MAP: img-01: [content type], img-02: [content type], ... (one line per image)
COVERAGE: [views present vs. missing, e.g. "front + back cover only; no spine-raking, no interior, no page-edge"]
SELLER DESCRIPTION NOTES: [any disclosed defects or condition notes from listing.html; "none stated" if clean]
GRADE: X.X (label) — best point estimate
GRADE RANGE: [plausible span given coverage, e.g. "5.0–6.0 VG/FN–FN"; may equal the point grade at HIGH confidence]
CONFIDENCE: HIGH | MEDIUM | MEDIUM-LOW | LOW — driven by coverage (state the one-line reason, e.g. "MEDIUM-LOW: 2 cover photos, no spine/interior/edge")
GRADE CAP: [defect that sets the ceiling, e.g. "spine split ~1/4" caps at 6.0 FN" — or "none" if no single cap applies]
SIGNATURE/CREDIT CHECK: [if any signature-like or credit text is visible, classify per the PRINT-LAYER RULE: "printed/facsimile — no effect", "authentic post-print autograph — writing defect", or "uncertain → treated as print-layer, not capped". State "none visible" if none.]
KEY DEFECTS OBSERVED:
- [front cover: ...]
- [spine: ...]
- [corners: ...]
- [back cover: ...]
- [pages/interior: ...]
- [staples: ...]
POSITIVES:
- [...]
RATIONALE: 2-3 sentences citing the grade-determining defects. Lead with physical defects; note whether reflectivity confirms or conflicts.
PHOTO LIMITATIONS: what you couldn't assess. Include restoration red flags if observed.
```

## Step 3: Synthesize Consensus

After all agents return for a given comic (1 grader if not escalated — see Step 2):

1. Collect the numeric grades, plus each grader's GRADE RANGE and CONFIDENCE.
2. Compute the average; note the spread.
3. If the graders agree within 0.5 pts → use the median as the point grade.
4. If spread is 1.0+ pts → read the outlier's rationale before defaulting to median, and classify WHY they disagree:
   - **Named-defect disagreement** (the outlier cites a *specific physical defect* the others missed — e.g. "spine split ~3/8"", "writing on story page 7"): the defect is likely real and the median is too high. Adopt the outlier's grade and flag the defect in the consensus.
   - **Lighting/reflectivity-only disagreement** (no named physical defect, just a brighter/duller read): discard the outlier and use the median.
   - **Epistemic disagreement** (the graders diverge because the photos don't *show* the deciding surface — nobody can name the defect because nobody can see that view): this is uncertainty, not a defect. Do NOT just take the median — set the consensus GRADE RANGE to span the disagreement and LOWER the consensus confidence accordingly. The point grade stays the median, but it travels with a wider range and a reduced confidence label.
5. **Consensus CONFIDENCE is capped by coverage** (which is identical across graders since they share the photos): start from the per-grader coverage ceiling, then lower it further if epistemic disagreement (case above) is present. A 2-cover-photo lot is MEDIUM-LOW at best no matter how tightly the graders agreed — agreement on insufficient data is not high confidence.
6. **Consensus GRADE RANGE** is the union of the graders' ranges, widened (not narrowed) by any epistemic disagreement.
7. Combine the defect lists (union, deduplicated) to produce a master defect summary.

### Consensus Table

```
| Grader | Grade | Range | Confidence |
|--------|-------|-------|------------|
| A      | X.X   | X.X–X.X | … |
| B      | X.X   | X.X–X.X | … |
| C      | X.X   | X.X–X.X | … |
| **Consensus** | **X.X (label)** | **X.X–X.X** | **HIGH/MEDIUM/MEDIUM-LOW/LOW** |
```

(Single-grader case: one row + the consensus row carrying that grader's grade, range, and coverage-capped confidence.)

## Output

Present one block per comic:

```
### Comic Title (Year) — Item ID
| Grader | Grade | Range | Confidence |
| A | 5.0 | 5.0–6.0 | MEDIUM-LOW |
| B | 5.0 | 5.0–5.5 | MEDIUM-LOW |
| C | 4.5 | 4.5–6.0 | MEDIUM-LOW |
| **Consensus** | **5.0 (VG/FN)** | **4.5–6.0** | **MEDIUM-LOW** |

Key defects: [2-3 sentence summary of the most important ones]
Positives: [brief]
Caveats: [what photos couldn't show]
```

Then a summary table at the end:

```
| # | Comic | Item ID | Consensus Grade | Range | Confidence |
|---|-------|---------|-----------------|-------|------------|
| 1 | FF #48 (1966) | 178057470740 | 5.0 VG/FN | 4.5–6.0 | MEDIUM-LOW |
| 2 | ASM #300 (1988) | 123456789 | 8.5 VF+ | 8.5 | HIGH |
```

The summary table is the input for `/comic:fmv`. **Carry the Confidence column forward** — `comic-fmv` consumes it (as `grade_confidence`) to haircut the bid cap when grade confidence is low (LOW/MEDIUM-LOW → `low`; MEDIUM → `medium`; HIGH → `high`).

## Integration with /comic:buy

`/comic:buy` accepts grades from this skill. After running `/comic:grade`, pass the consensus grade column directly into the FMV step:

> "Using these grades, for these URLs" → triggers `/comic:buy` to skip Step 1's seller-stated grade and use the photo-assessed grades instead.

## Caveats to Always State

These are structural limitations of any photo-based assessment:

- No close-up of staple shanks → rust unknown
- No centerfold spread photo → attachment confidence only
- No raking-light shot → subtle color-breaking creases may be missed
- No flex test → brittleness unknown
- No black-light → color touch / restoration not detectable
- Actual CGC grade could land ±0.5 from this assessment; restoration discovery would drop it more

Always note these. Do not claim CGC accuracy.

## Common Mistakes

| Mistake | Fix |
|---------|-----|
| Scraping eBay HTML for images | Use the Browse API (`get_item_by_legacy_id`) — returns `image` + `additionalImages` URLs directly, no bot detection |
| Using firecrawl/WebFetch for eBay images | Both are blocked by eBay bot detection — use Browse API only |
| Grading from WebFetch text output | WebFetch returns markdown text, not images — useless for visual grading |
| Giving all 3 agents the same agent name | Use distinct names (e.g., `grader-c1-a`, `grader-c1-b`) so results are traceable |
| Running graders sequentially | Within a batch, dispatch in a single message for independence — batch 1 = one first-grader per comic; batch 2 = the extra 2 graders for comics that tripped the value gate |
| Fanning out 3 graders for every comic | Value-gate it (Step 2): 1 grader first, escalate to 3 only on value ≥ threshold or an ambiguous/near-cap grade. State the grader count + reason per comic |
| Including related-listing images | Extract carousel IDs from the `ux-image-carousel-container` section only |
| Inflating grade because it's a key issue | Grade physical condition only — key issue premium belongs in FMV, not grade |
| Capping the grade over a printed credit/signature | Printed credits, facsimile signatures, barcodes, and price boxes are in the print layer — never defects (PRINT-LAYER RULE). Only a post-print autograph caps. When unsure, do NOT cap |
| Skipping the caveat section | Always disclaim photo-based limitations so user knows the confidence level |
