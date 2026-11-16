#!/usr/bin/env python3
"""slab-checks: pre-alert checks on one eBay slab listing (BUI-1418).

Reads ONLY what the eBay Browse item shows (`get_item_by_legacy_id`). It never
calls any CGC site: CGC's terms forbid automated cert lookups.

Checks, and the verdict:

  * seller  - `seller.feedbackPercentage` and `seller.feedbackScore`. "Weak"
              when feedback % is below SLAB_MIN_FEEDBACK_PCT (default 98.0) or
              score is below SLAB_MIN_FEEDBACK_SCORE (default 50).
  * returns - `returnTerms.returnsAccepted`. No returns -> drop.
  * cert    - cert number, grader, and grade from `localizedAspects`
              ("Certification Number", "Professional Grader", "Grade"); the
              grader falls back to the title (CGC/CBCS/PGX). No cert number ->
              the listing passes, marked unverified.

Verdict: `drop` (reasons listed), `pass`, or `pass_unverified`.

Safe reading of missing data: a field eBay does not return is NOT read as
good. A missing `returnTerms.returnsAccepted` counts as "no returns" and a
missing feedback percentage or score counts as weak, each with a reason that
says "unknown". A false drop costs one look at a listing; a false pass can cost
a book from a seller with no recourse.

Env overrides (also shown in --help):
    SLAB_MIN_FEEDBACK_PCT    default 98.0
    SLAB_MIN_FEEDBACK_SCORE  default 50

Usage (from apps/ebay):
    uv run python src/slab_checks.py <item_id_or_url>... [--json]
Importable: `check_listing(item_dict) -> dict` is pure (no network).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from typing import Any

DEFAULT_MIN_FEEDBACK_PCT = 98.0
DEFAULT_MIN_FEEDBACK_SCORE = 50

_CERT_KEYS = ("certification number", "cert number", "certification #", "cert #")
_GRADER_KEYS = ("professional grader", "grader", "certification")
_GRADE_KEYS = ("grade",)
_TITLE_GRADER_RE = re.compile(r"\b(CGC|CBCS|PGX)\b", re.IGNORECASE)
_CERT_NUM_RE = re.compile(r"\d")


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw)
    except ValueError:
        raise SystemExit(f"Error: {name}={raw!r} is not a number") from None


def thresholds() -> tuple[float, float]:
    return (_env_float("SLAB_MIN_FEEDBACK_PCT", DEFAULT_MIN_FEEDBACK_PCT),
            _env_float("SLAB_MIN_FEEDBACK_SCORE", DEFAULT_MIN_FEEDBACK_SCORE))


def _to_float(v: Any) -> float | None:
    if isinstance(v, bool) or v is None:
        return None
    try:
        return float(str(v).strip().rstrip("%"))
    except ValueError:
        return None


def _aspects(item: dict) -> dict[str, str]:
    """Flatten localizedAspects ([{name, value}]) to {lowercased name: value}."""
    out: dict[str, str] = {}
    raw = item.get("localizedAspects")
    if isinstance(raw, list):
        for a in raw:
            if isinstance(a, dict) and a.get("name") and a.get("value") not in (None, ""):
                out[str(a["name"]).strip().lower()] = str(a["value"]).strip()
    return out


def _first(aspects: dict[str, str], keys: tuple[str, ...]) -> str | None:
    for k in keys:
        if aspects.get(k):
            return aspects[k]
    return None


def check_seller(item: dict, min_pct: float, min_score: float) -> dict:
    seller = item.get("seller") if isinstance(item.get("seller"), dict) else {}
    pct = _to_float(seller.get("feedbackPercentage"))
    score = _to_float(seller.get("feedbackScore"))
    reasons = []
    if pct is None:
        reasons.append("seller feedback percentage unknown")
    elif pct < min_pct:
        reasons.append(f"seller feedback {pct:g}% below {min_pct:g}%")
    if score is None:
        reasons.append("seller feedback score unknown")
    elif score < min_score:
        reasons.append(f"seller feedback score {score:g} below {min_score:g}")
    return {"username": seller.get("username"), "feedback_pct": pct,
            "feedback_score": None if score is None else int(score),
            "weak": bool(reasons), "reasons": reasons}


def check_returns(item: dict) -> dict:
    terms = item.get("returnTerms")
    accepted = terms.get("returnsAccepted") if isinstance(terms, dict) else None
    if accepted is True:
        return {"accepted": True, "reasons": []}
    if accepted is False:
        return {"accepted": False, "reasons": ["returns not accepted"]}
    return {"accepted": None,
            "reasons": ["returns unknown (no returnTerms), treated as no returns"]}


def check_cert(item: dict) -> dict:
    aspects = _aspects(item)
    number = _first(aspects, _CERT_KEYS)
    if number is not None and not _CERT_NUM_RE.search(number):
        number = None  # "N/A", "Does not apply", ...
    grader = _first(aspects, _GRADER_KEYS)
    if grader is None:
        m = _TITLE_GRADER_RE.search(str(item.get("title") or ""))
        grader = m.group(1).upper() if m else None
    return {"number": number, "grader": grader,
            "grade": _first(aspects, _GRADE_KEYS), "verified": number is not None}


def check_listing(item: dict, *, min_pct: float | None = None,
                  min_score: float | None = None) -> dict:
    """Pure: Browse item dict in, structured verdict out. No network."""
    if min_pct is None or min_score is None:
        env_pct, env_score = thresholds()
        min_pct = env_pct if min_pct is None else min_pct
        min_score = env_score if min_score is None else min_score
    item = item if isinstance(item, dict) else {}
    seller = check_seller(item, min_pct, min_score)
    returns = check_returns(item)
    cert = check_cert(item)
    reasons = seller["reasons"] + returns["reasons"]
    if reasons:
        verdict = "drop"
    elif cert["number"] is None:
        verdict = "pass_unverified"
    else:
        verdict = "pass"
    return {"item_id": item.get("legacyItemId") or item.get("itemId"),
            "title": item.get("title"), "verdict": verdict, "reasons": reasons,
            "seller": seller, "returns": returns, "cert": cert}


def render(results: list[dict]) -> str:
    rows = [("item", "verdict", "seller", "returns", "cert", "reasons")]
    for r in results:
        if r.get("error"):
            rows.append((str(r["item_id"]), "error", "", "", "", r["error"]))
            continue
        s, c = r["seller"], r["cert"]
        pct = s["feedback_pct"] if s["feedback_pct"] is not None else "?"
        score = s["feedback_score"] if s["feedback_score"] is not None else "?"
        ret = {True: "yes", False: "no", None: "unknown"}[r["returns"]["accepted"]]
        cert = " ".join(x for x in (c["grader"], c["grade"], c["number"]) if x) or "none"
        rows.append((str(r["item_id"]), r["verdict"], f"{pct}% / {score}", ret, cert,
                     "; ".join(r["reasons"])))
    w = [max(len(row[i]) for row in rows) for i in range(5)]
    return "\n".join("  ".join(row[i].ljust(w[i]) for i in range(5)) + "  " + row[5]
                     for row in rows).rstrip()


def run(args_ids: list[str]) -> list[dict]:
    from ebay_fetch import extract_item_id, fetch_item_with_status, get_token, load_config
    client_id, client_secret, base_url = load_config()
    token = get_token(client_id, client_secret, base_url)
    out = []
    for arg in args_ids:
        item_id = extract_item_id(arg)
        data, status = fetch_item_with_status(item_id, token, base_url)
        if data is None:
            out.append({"item_id": item_id, "verdict": None,
                        "error": f"fetch failed (HTTP {status})"})
        else:
            out.append(check_listing(data))
    return out


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(
        prog="ebay-slab-check",
        description="Pre-alert checks on eBay slab listings: seller feedback, returns, "
                    "cert. Reads only the eBay listing; never calls a CGC site.",
        epilog="Env: SLAB_MIN_FEEDBACK_PCT (default 98.0) and SLAB_MIN_FEEDBACK_SCORE "
               "(default 50) set the weak-seller thresholds. Missing returnTerms is "
               "treated as no returns; missing feedback as weak.")
    p.add_argument("items", nargs="+", metavar="ITEM_ID_OR_URL")
    p.add_argument("--json", action="store_true", help="JSON instead of a table")
    args = p.parse_args(argv)
    thresholds()  # fail fast on a bad env value
    results = run(args.items)
    print(json.dumps(results, indent=2) if args.json else render(results))
    if any(r.get("error") for r in results):
        sys.exit(1)


if __name__ == "__main__":
    main()
