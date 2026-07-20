---
title: "Broadening the comic-identify annual classifier: ReDoS, year-numbered-annual, and separator-strip hazards"
date: 2026-07-20
category: docs/solutions/best-practices
module: "apps/ebay comic-identify (comic_identity_year.py, comic_identity.py)"
problem_type: best_practice
component: service_object
severity: medium
applies_when:
  - "Editing _ANNUAL_EDITION_RE / _classify_edition_kind in apps/ebay/src/comic_identity_year.py"
  - "Broadening any comic-identity regex to accept more separators or tokens"
  - "Adding a heuristic that keys off a year-like digit run in a comic title"
  - "Stripping a matched token (and any separator around it) back out of the series text after classification"
tags:
  - comic-identify
  - regex
  - redos
  - annual
  - apps-ebay
  - bui-460
related_docs:
  - "docs/solutions/ui-bugs/collection-check-alias-and-printing-false-positives.md"
  - "docs/solutions/architecture-patterns/evidence-layer-disambiguation-vs-heuristic-gating.md"
  - "docs/solutions/design-patterns/guard-strictness-must-match-consequence.md"
---

# Broadening the comic-identify annual classifier: ReDoS, year-numbered-annual, and separator-strip hazards

## Context

`_classify_edition_kind` in `apps/ebay/src/comic_identity_year.py` decides whether an eBay title is an "annual" edition. BUI-450 tightened it so an issue-number token must **follow** the word (`_ANNUAL_EDITION_RE = r"\bannual\b\s*#?\s*\d"`). BUI-456 closed two remaining adjacency edge cases in the same regex, broadening the separator it accepts between "annual" and its issue number to `":"`, `"-"`, `"#"`, and `"No."`/`"No"`. BUI-460 then found that the broadened separator, once accepted for *classification*, was left orphaned in the *series text* by the older word-only strip — a third trap in the same file, same shape as the first two: broadening what a regex matches without equally reconsidering what surrounds the match.

## Guidance

### 1. Broaden a separator with a single character class, not stacked `\s*` runs

Widening `\s*#?\s*\d` to also accept `No.` / `:` / `-` by writing alternation between optional whitespace runs — e.g. `\s*(?:no\.?|[:#-])?\s*#?\s*\d` — puts **three adjacent `\s*` runs** next to each other. They can partition a run of whitespace ambiguously, which is classic catastrophic backtracking: a title with a long space run hangs (a 5000-space input ran >120s).

Collapse the whole gap to **one character-class quantifier** followed by the digit:

```python
# ReDoS-safe: one class, one quantifier, then the required digit.
_ANNUAL_EDITION_RE = re.compile(r"\bannual\b[\s#:.no-]*\d", re.IGNORECASE)
```

This is provably linear (verified to 500k chars in ~7ms). Keep **word-internal letters out of the class** so the run can't leap across a following word to a distant digit — `"annual note 5"`, `"annual-versary 3"` must stay single-issue (the `t`/`v`/`e` break the run before the digit).

### 2. Don't key an annual guard on "a 4-digit year after the word"

The tempting fix for the promo-year false positive (`"ASM #252 annual 2024 sale"` mis-filing as a phantom `ASM Annual #252`) is to reject a 4-digit year right after "annual". **That breaks real year-numbered annuals** — IDW `Sonic the Hedgehog Annual 2022/2023`, UK year-annuals, and bare-volume-year forms like `Star Wars 2021 Annual #1`.

Key instead on a **`#N` issue token appearing *before* the word "annual"**. A genuine annual names its series first and never precedes "annual" with a `#`-issue token (volume years are bare — `2021 Annual`, never `#2021`). So a preceding `#N` is positive evidence that the digit *after* "annual" is a promo/marketing year:

```python
_ISSUE_HASH_RE = re.compile(r"#\s*\d")
annual_m = _ANNUAL_EDITION_RE.search(title)
if annual_m and not _ISSUE_HASH_RE.search(title, 0, annual_m.start()):
    return "annual"
```

### 3. Anchor a strip regex to the token being removed, not to the end of the string

BUI-456's broadened separator fixed *classification* — `"X-Men Annual: 1"` now correctly
detects `edition="annual"`. But the older strip that removes the word "annual" from the
*series* text (`_ANNUAL_RE.sub("", pre_issue_text)` in `apps/ebay/src/comic_identity.py`)
only ever deleted the bare word, leaving the now-accepted separator orphaned:
`"X-Men Annual: 1"` produced series `"X-Men :"` instead of `"X-Men"`. That mattered
downstream — `collection_cache._normalize_series_key` only strips year/vol/article
decoration, not stray punctuation, so the uncleaned series threaded to the wrong
`norm_key` and missed the local `series_name_index`.

The tempting fix is a second regex that trims trailing separator-looking characters —
but written with no anchor to "annual" itself, keyed only on "the end of the string":

```python
# WRONG — anchored only to end-of-string, not to the word being removed:
_TRAILING_SEP_RE = re.compile(r"[\s#:.no-]*$", re.IGNORECASE)
```

This silently ate a real trailing **word** out of series names whenever it happened to be
`"No"` — `"Just Say No Annual #1"` → strip "Annual" → `"Just Say No "` → this second pass
eats the `"No"` too → `"Just Say"`. Once "annual" is deleted, text that legitimately
*precedes* "annual" in the title (`"No"` in `"Just Say No"`) and separator residue that
*follows* "annual" (`"No."` in `"Annual No. 1"`) become indistinguishable from
end-of-string alone — the character class `[\s#:.no-]` matches letters that spell real
words, not just punctuation.

The fix anchors the strip to the word itself and only extends **forward**, reusing the
identical separator character class `_ANNUAL_EDITION_RE` already validates as a
classification precondition:

```python
_ANNUAL_WORD_AND_SEP_RE = re.compile(r"\bannual\b[\s#:.no-]*$", re.IGNORECASE)
```

`\bannual\b` fixes the deletion's left edge to the word being removed; `$` still requires
the separator run to reach the end of the (already issue-number-truncated) series text,
but only text **after** "annual" can ever match — nothing before it is touched, so
`"Just Say No Annual #1"` correctly yields `"Just Say No"`.

## Why This Matters

- **The ReDoS is invisible in unit tests** — normal-length titles pass instantly; only a pathological input reveals it. A single-character-class rewrite removes the failure mode by construction rather than hoping no such title arrives.
- **Year-numbered annuals are a real, published shape**, not a corner case. A year-rejection heuristic silently demotes them to single-issue and mis-files them. The `#`-before-annual key closes the damaging misfile (an existing issue number reclassified as a nonexistent annual) while preserving those annuals.
- **Both classifier error directions here are recoverable (duplicate-buy), never false-ownership-of-a-valuable-key** — that is what justifies accepting the residual below rather than chasing zero error.
- **A strip regex with no anchor to the token it's removing has no way to know where "removed" ends and "adjacent real text" begins.** Once the token is gone, everything remaining looks the same to an end-of-string match — an anchor to the token itself (not just a character class of "separator-looking" characters) is what keeps the deletion scoped to residue the token actually produced.

## When to Apply

Any edit to `_ANNUAL_EDITION_RE` / `_classify_edition_kind`, or any new comic-identity heuristic that inspects a digit run — re-verify against `test_comic_identify.py::TestComicIdentifyAnnualAdjacency` (both directions) and confirm seller-scan's `hard_reject` is unaffected, since it uses a **separate** `_EDITION_PATTERNS` in `comic_identity.py`, not `_classify_edition_kind`. Also any edit to a strip/cleanup regex that removes a classified token's text back out of a string that will be re-parsed downstream (here, the series text feeding `_normalize_series_key`) — anchor the strip to the token, verify it only ever extends toward the token's own separator, and add an adversarial test for a series name that legitimately ends in one of the separator class's characters spelled out as a word (e.g. a title ending in "No").

## Examples

Accepted residual (deliberately not closed): the bare-number-before variant `"ASM 252 annual 2024"` (no `#`) still classifies as annual, because distinguishing a bare issue number from a bare volume year reintroduces the BUI-129 hazard. Lock the accepted false-negative in a named test so the trade-off is explicit rather than silently re-"fixed" later.

| Title | Result |
|-------|--------|
| `ASM #252 annual 2024 sale` | single-issue (guard: `#252` precedes "annual") |
| `X-Men Annual #1` | annual, series `"X-Men"` |
| `Annual No. 1` / `Annual: 1` / `Annual-#5` | annual (broadened separator) |
| `Star Wars 2021 Annual #1` | annual (bare volume-year preserved) |
| `annual note 5` | single-issue (word-internal letters break the run) |
| `X-Men Annual: 1` | series `"X-Men"` (BUI-460: separator stripped with "annual", not orphaned) |
| `Just Say No Annual #1` | series `"Just Say No"` (BUI-460: anchored strip never touches text preceding "annual") |
