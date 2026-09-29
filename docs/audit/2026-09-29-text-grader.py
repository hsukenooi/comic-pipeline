# /// script
# requires-python = ">=3.12"
# dependencies = ["scikit-learn"]
# ///
"""Grade the 150-comp BUI-1006 holdout from listing TEXT alone (BUI-1015).

DIAGNOSTIC ONLY. No Browse calls, no ledger or DB writes. Inputs come from the
BUI-1012 holdout cache (--holdout: manifest.json, full/<product_id>/item.json,
bui-1006/joined.json, bui-1011/preds_l14.json) and BUI-1013's preds json
(--bui1013: a dir holding preds_l14_both.json).

  prepare   Strip HTML, redact every stated grade, write redacted.json (rid ->
            text, no grade), batches/batch-NN.json (15 listings each, random
            order) and print coverage: how many listings keep condition prose
            by pool, the median remaining words, an independent residual-leak
            check, and sample20.txt (20 random redacted texts to eyeball).
  validate  Check every Haiku batch file (grades/batch-NN.jsonl); list the
            batches that need a re-ask.
  jev       Ask TypeSafe's Jev (a Choice over grade bands; stdlib HTTP; key
            from TYPESAFE_API_KEY or apps/ebay/.env in the shared checkout,
            never printed) about every redacted listing -> preds_jev.json.
  score     Join validated Haiku and Jev predictions to the truth and print
            the BUI-1006 table (MAE, +-1.0, +-0.5, bias, by band, raw vs
            slab, QWK), paired fixes/breaks against Haiku-photo, BUI-1011 and
            BUI-1013, and the selective coverage curve.

    uv run python docs/audit/2026-09-29-text-grader.py prepare --workdir DIR

Haiku grading (no API key here): each batch file is graded by one
Agent(subagent_type="general-purpose", model="haiku") that Reads
batches/batch-NN.json and Writes one JSON line per listing
{"rid","grade","confidence"} to grades/batch-NN.jsonl (HAIKU_PROMPT below).
`validate` rejects a batch whose file is missing, malformed or incomplete; the
caller re-asks that batch and logs the count. The rid-to-grade key is never
sent to a grader.

Coverage-confidence rule (fixed BEFORE looking at any holdout result): Haiku
high=2 / medium=1 / low=0, ties in a fixed seeded order; Jev = the Choice
answer's own confidence. Nothing is tuned on the holdout.
"""
from __future__ import annotations

import argparse
import html
import json
import os
import random
import re
import statistics
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

REPO_MAIN = Path("/Users/hsukenooi/Projects/comic-pipeline")
API_URL = "https://api.typesafe.ai/v1/systemone"
MODEL = "jev-latest"
BATCH = 15
DESC_CAP = 2500  # chars of description kept after cleaning
RAW_BANDS = [(0.5, 3.5), (4.0, 5.5), (6.0, 7.5), (8.0, 8.5), (9.0, 9.2), (9.4, 10.0)]
SLAB_BANDS = [(2.0, 3.5), (4.0, 5.5), (6.0, 7.5), (8.0, 8.5), (9.0, 9.4), (9.6, 10.0)]
LADDER = [x / 2 for x in range(1, 19)] + [9.2, 9.4, 9.6, 9.8, 10.0]

# Jev answers a Choice over these bands; the prediction is the band midpoint.
JEV_BANDS = {
    "b1_0_5_to_1_5": (1.0, "Poor to Fair: heavily damaged, major pieces missing, detached or coverless, large tears, heavy staining"),
    "b2_2_to_3": (2.5, "Good: heavy wear, large creases, tears or chips, spine splits, but complete"),
    "b3_3_5_to_4_5": (4.0, "Very Good: worn and used, multiple creases, rounded corners, spine roll, small tears"),
    "b4_5_to_6": (5.5, "Fine-ish: noticeable wear, spine stress lines, soft corners, minor creases, some gloss loss"),
    "b5_6_5_to_7_5": (7.0, "Fine to Very Fine: light wear, a few small creases or stress lines, good gloss"),
    "b6_8_to_8_5": (8.25, "Very Fine: minor wear, a small stress line or slightly blunted corner, glossy"),
    "b7_9_to_9_2": (9.1, "Very Fine/Near Mint: nearly flat and glossy, a few tiny flaws"),
    "b8_9_4": (9.4, "Near Mint: flat, glossy, sharp corners, only tiny flaws"),
    "b9_9_6_up": (9.7, "Near Mint/Mint: essentially flawless, sharp, pristine"),
}

# ------------------------------------------------------------------ redaction

_GR = r"(?:NM|VF|FN|VG|GD|FR|PR|MT|GM)"
_SGL = r"(?:F|G)"
_HTML = [
    (re.compile(r"<(script|style)[^>]*>.*?</\1>", re.S | re.I), " "),
    (re.compile(r"<[^>]+>"), " "),
]
_WORDS = (r"(?:gem\s+mint|near[\s\-]+mint|very[\s\-]+fine|very[\s\-]+good|fine|good|fair|poor|mint|like\s+new)")
_REDACT = [
    # slab label text and certifier names (with any number that follows)
    re.compile(r"\b(?:CGC|CBCS|PGX|CGS)\b(?:\s*[:#]?\s*\d+(?:\.\d+)?)?", re.I),
    re.compile(r"\b(?:certified|certification|cert(?:ificate)?|universal|qualified|signature\s+series|"
               r"encapsulated|slabbed|graded\s+by)\b(?:\s*(?:no\.?|number|#)?\s*[:#]?\s*\d+)?", re.I),
    re.compile(r"\b\d{7,12}\b"),  # cert numbers and the like
    # numeric grades: 9.8, 4.5, 10.0 (also inside ranges and prices; over-redaction is fine)
    re.compile(r"(?<![\d.])(?:10|\d)\.\d{1,2}(?!\d)"),
    re.compile(r"\b\d{1,2}\s*/\s*10\b"),
    # "grade 8", "graded at 9", "grades in at a 6"
    re.compile(r"\b(?:grades?|graded|grading|condition|rated|rating)\s*(?:of|is|at|in|as|:|-)?\s*(?:at|a|an)?\s*[:\-]?\s*\d{1,2}\b", re.I),
    # spelled-out grade words, with +/- and slash combos
    re.compile(rf"\b{_WORDS}\b(?:\s*[/+\-]\s*{_WORDS}\b)*[+\-]?", re.I),
    # abbreviations with optional +/- and slash combos
    re.compile(rf"\b{_GR}\b(?:\s*[+\-](?!\w))?(?:\s*/\s*(?:{_GR}|{_SGL})\b(?:\s*[+\-](?!\w))?)*", re.I),
    # single F / G only when attached to a sign or slash combo, or in parentheses
    re.compile(rf"(?<![A-Za-z])/?\s*\b{_SGL}\b\s*(?:[+\-](?!\w)|/\s*(?:{_GR}|{_SGL})\b)"),
    re.compile(rf"\({_SGL}[+\-]?\)"),
    # stubs left when a combo was only half removed: "F/", "G/ ,", "( /M)", "( - )"
    re.compile(r"(?<![A-Za-z0-9])[A-Z]{1,2}\s*/\s*(?![A-Za-z0-9])"),
    re.compile(r"(?<![A-Za-z0-9])/\s*[A-Z]{1,2}(?![A-Za-z0-9])"),
    re.compile(r"\(\s*[/+\-]*\s*\)"),
]
_RESIDUAL = [  # independent post-check: any of these surviving is a leak
    re.compile(r"\b(?:CGC|CBCS|PGX|CGS)\b", re.I),
    re.compile(rf"\b{_GR}\b", re.I),
    re.compile(r"(?<![\d.])(?:10|\d)\.\d"),
    re.compile(rf"\b{_WORDS}\b", re.I),
    re.compile(r"\b\d{7,12}\b"),
    re.compile(r"\b\d{1,2}\s*/\s*10\b"),
    re.compile(r"\b(?:grades?|graded)\s+(?:at\s+|in\s+at\s+|of\s+)?(?:a\s+)?\d{1,2}\b(?![\-/]\d)", re.I),
    re.compile(r"(?<![A-Za-z0-9])[A-Z]{1,2}\s*/\s*(?![A-Za-z0-9])"),  # half-removed combo stubs
    re.compile(r"(?<![A-Za-z0-9])/\s*[A-Z]{1,2}(?![A-Za-z0-9])"),
]
CONDITION_WORDS = re.compile(
    r"\b(?:crease[sd]?|creasing|wear|worn|tear[s]?|torn|spine|corner[s]?|gloss\w*|pages?|staple[sd]?|clean|flat|"
    r"tape[d]?|stain\w*|chip\w*|fold[s]?|folded|minor|split[s]?|rip\w*|scuff\w*|foxing|yellow\w*|brown\w*|"
    r"tanning|toned|bend[s]?|bent|roll\w*|stress|blunt\w*|sharp|bright|vibrant|pristine|unread|flaw[s]?|defect[s]?|"
    r"damage[d]?|missing|detached|loose|writing|marks?|marked|dent[s]?|abrasion[s]?|brittle|supple|"
    r"restor\w*|pressed|fade[d]?|smudge\w*|dirt\w*|spotting|discolou?r\w*|edge\s+wear)\b", re.I)


def strip_html(s: str) -> str:
    for rx, rep in _HTML:
        s = rx.sub(rep, s or "")
    return re.sub(r"\s+", " ", html.unescape(s)).strip()


def redact(s: str) -> str:
    s = strip_html(s)
    for _ in range(2):  # a second pass catches residue exposed by the first
        for rx in _REDACT:
            s = rx.sub(" ", s)
    s = re.sub(r"\(\s*\)|\[\s*\]", " ", s)
    return re.sub(r"\s+", " ", s).strip(" ,;:-/")


def leaks(s: str) -> list[str]:
    return [rx.pattern[:24] for rx in _RESIDUAL if rx.search(s)]


def build_text(item: dict) -> dict:
    """Redacted title, seller condition note and cleaned description. eBay's
    categorical `condition` enum (Very Good, Like New, ...) is itself a grade
    word, so it is withheld."""
    return {"title": redact(item.get("title") or ""),
            "cond": redact(item.get("conditionDescription") or ""),
            "desc": redact(item.get("description") or "")[:DESC_CAP]}


def render(t: dict) -> str:
    parts = [f"Title: {t['title']}"]
    if t["cond"]:
        parts.append(f"Seller condition note: {t['cond']}")
    if t["desc"]:
        parts.append(f"Description: {t['desc']}")
    return "\n".join(parts)


# ------------------------------------------------------------------ prepare

HAIKU_PROMPT = """You grade comic books from a seller's listing text alone (no photos). Every stated \
grade has been removed from the text, so estimate the physical condition from the condition wording \
that is left (wear, creases, spine, corners, gloss, pages, tears, tape, and so on). Some listings \
say nothing about condition; guess from the era and anything else you can read, and give low confidence.

Scale: CGC/Overstreet 0.5-10.0 in 0.5 steps (9.2, 9.4, 9.6, 9.8 also allowed). 9.4 NM: flat, glossy, \
tiny flaws only. 8.0 VF: minor wear. 6.0 FN: noticeable wear, spine stress, soft corners. 4.0 VG: worn, \
multiple creases, rounded corners. 2.0 GD: heavy wear, big creases, tears, chips.

Use the Read tool to open this file: {batch_path}
It is a JSON list of {{"rid", "text"}} objects. Do not guess from the rid. Then use the Write tool to write \
exactly one JSON line per listing to {out_path}, in the form:
{{"rid": "<rid>", "grade": <number>, "confidence": "high|medium|low"}}
Write nothing else to the file. When done, reply with only one line: DONE <number of lines written>"""


def cmd_prepare(wd: Path, hd: Path) -> None:
    man = json.loads((hd / "manifest.json").read_text())["comps"]
    assert len(man) == 150
    rows = []
    for c in man:
        it = json.loads((hd / "full" / c["product_id"] / "item.json").read_text())
        t = build_text(it)
        body = " ".join([t["cond"], t["desc"]]).strip()
        rows.append({"rid": c["rid"], "pool": c["pool"], "t": t, "text": render(t),
                     "body_words": len(body.split()),
                     "cond_hits": len(CONDITION_WORDS.findall(body)),
                     "leaks": leaks(render(t))})
    wd.mkdir(parents=True, exist_ok=True)
    (wd / "redacted.json").write_text(json.dumps({r["rid"]: r["text"] for r in rows}, indent=1))
    (wd / "key.json").write_text(json.dumps({c["rid"]: c for c in man}, indent=1))
    order = list(rows)
    random.Random(1015).shuffle(order)
    (wd / "batches").mkdir(exist_ok=True)
    (wd / "grades").mkdir(exist_ok=True)
    nb = 0
    for i in range(0, len(order), BATCH):
        (wd / "batches" / f"batch-{nb:02d}.json").write_text(
            json.dumps([{"rid": r["rid"], "text": r["text"]} for r in order[i:i + BATCH]], indent=1))
        nb += 1
    (wd / "haiku_prompt.txt").write_text(HAIKU_PROMPT)
    print(f"{nb} batches of <= {BATCH}")
    print("\ncoverage after redaction (body = seller condition note + description; title excluded)")
    print("| Pool | n | any body words | condition-word hit >=1 | hit >=3 | median body words (hit >=1) | residual auto-leaks |")
    print("|---|---|---|---|---|---|---|")
    for pool in ("raw", "slab", "all"):
        s = [r for r in rows if pool in ("all", r["pool"])]
        hit = [r for r in s if r["cond_hits"] >= 1]
        med = statistics.median(r["body_words"] for r in hit) if hit else 0
        print(f"| {pool} | {len(s)} | {sum(r['body_words'] > 0 for r in s)} | {len(hit)} | "
              f"{sum(r['cond_hits'] >= 3 for r in s)} | {med:.0f} | {sum(bool(r['leaks']) for r in s)} |")
    note = [r for r in rows if r["t"]["cond"]]
    print(f"seller condition note present: {len(note)} (raw {sum(r['pool'] == 'raw' for r in note)}, "
          f"slab {sum(r['pool'] == 'slab' for r in note)})")
    for r in rows:
        if r["leaks"]:
            print("AUTO-LEAK", r["rid"], r["leaks"])
    pick = random.Random(7).sample(rows, 20)
    (wd / "sample20.txt").write_text("\n\n".join(f"[{r['rid']} {r['pool']}]\n{r['text'][:700]}" for r in pick))
    (wd / "coverage.json").write_text(json.dumps(
        {r["rid"]: {"pool": r["pool"], "words": r["body_words"], "hits": r["cond_hits"]} for r in rows}))


# ------------------------------------------------------------------ Haiku validation

def load_haiku(wd: Path) -> tuple[dict[str, dict], list[int]]:
    """Validated Haiku predictions and the indexes of batches that failed."""
    preds, bad = {}, []
    for bf in sorted((wd / "batches").glob("batch-*.json")):
        idx = int(bf.stem.split("-")[1])
        want = {x["rid"] for x in json.loads(bf.read_text())}
        f = wd / "grades" / f"{bf.stem}.jsonl"
        got: dict[str, dict] = {}
        if f.exists():
            for line in f.read_text().splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                    g = float(d["grade"])
                    assert d["rid"] in want and 0.5 <= g <= 10.0 and d["confidence"] in ("high", "medium", "low")
                    got[d["rid"]] = {"pred": g, "conf": d["confidence"]}
                except (ValueError, KeyError, AssertionError, TypeError):
                    got = {}
                    break
        if set(got) == want:
            preds.update(got)
        else:
            bad.append(idx)
    return preds, bad


def cmd_validate(wd: Path) -> None:
    preds, bad = load_haiku(wd)
    print(f"valid listings {len(preds)}/150; batches needing a re-ask: {bad}")


# ------------------------------------------------------------------ Jev

def _api_key() -> str:
    key = os.environ.get("TYPESAFE_API_KEY")
    if not key:
        env = REPO_MAIN / "apps" / "ebay" / ".env"
        if env.exists():
            for line in env.read_text().splitlines():
                if line.startswith("TYPESAFE_API_KEY="):
                    key = line.split("=", 1)[1].strip()
    if not key:
        sys.exit("TYPESAFE_API_KEY not set (env or apps/ebay/.env): run Haiku alone")
    return key


QUESTION = {
    "grade_band": {
        "type": "choice",
        "instructions": {
            "question": "From the condition wording in `listing` alone, which grade band does the physical "
                        "condition of this comic book fall in?",
            "note": "Every stated grade was removed from the text. Judge from wear words (creases, spine, "
                    "corners, gloss, pages, tears, tape, stains). If the listing says nothing about condition, "
                    "pick the most likely band from the era and any other cues.",
        },
        "criteria": {k: d for k, (_, d) in JEV_BANDS.items()},
    }
}


def ask_jev(rid: str, text: str, key: str) -> dict:
    body = json.dumps({"model": MODEL, "state": {"listing": text}, "questions": QUESTION}).encode()
    for attempt in range(6):
        req = urllib.request.Request(API_URL, body, {"Authorization": f"Bearer {key}",
                                                     "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                a = json.load(r)["answers"]["grade_band"]
            probs = a.get("probabilities") or {}
            ev = sum(JEV_BANDS[k][0] * p for k, p in probs.items() if k in JEV_BANDS)
            return {"rid": rid, "choice": a["choice"], "pred": JEV_BANDS[a["choice"]][0],
                    "ev": ev, "conf": a["confidence"], "probs": probs}
        except urllib.error.HTTPError as e:
            if e.code not in (429, 500, 502, 503, 529) or attempt == 5:
                raise
        except (urllib.error.URLError, TimeoutError):
            if attempt == 5:
                raise
        time.sleep(2 ** attempt)
    raise RuntimeError("unreachable")


def cmd_jev(wd: Path) -> None:
    red = json.loads((wd / "redacted.json").read_text())
    key = _api_key()
    with ThreadPoolExecutor(4) as ex:
        out = list(ex.map(lambda kv: ask_jev(kv[0], kv[1], key), red.items()))
    (wd / "preds_jev.json").write_text(json.dumps({o["rid"]: o for o in out}, indent=1))
    print(f"jev answered {len(out)}/{len(red)}")


# ------------------------------------------------------------------ score

def rung(g: float) -> int:
    return min(range(len(LADDER)), key=lambda i: (abs(LADDER[i] - g), -i))


def _row(pairs) -> str:
    if not pairs:
        return "| 0 | - | - | - | - | - | - |"
    from sklearn.metrics import cohen_kappa_score
    p, t = [a for a, _ in pairs], [b for _, b in pairs]
    err = [a - b for a, b in pairs]
    ae = [abs(e) for e in err]
    varied = len(set(p)) > 1 and len(set(t)) > 1
    r = statistics.correlation(p, t) if varied else float("nan")
    k = (cohen_kappa_score([rung(x) for x in p], [rung(x) for x in t], weights="quadratic",
                           labels=list(range(len(LADDER)))) if varied else float("nan"))
    return (f"| {len(pairs)} | {statistics.mean(ae):.2f} | {sum(a <= 1.0 for a in ae) / len(ae):.0%} | "
            f"{sum(a <= 0.5 for a in ae) / len(ae):.0%} | {statistics.mean(err):+.2f} | {r:.2f} | {k:.2f} |")


def _hit(r, k="pred"):
    return abs(r[k] - r["grade"]) <= 1.0


def _coverage(rows, fracs=(0.25, 0.40, 0.5, 0.75, 1.0)):
    rows = sorted(rows, key=lambda r: -r["cv"])  # stable sort: ties keep the seeded shuffle order
    out = []
    for f in fracs:
        n = max(1, round(f * len(rows)))
        out.append((f, n, sum(map(_hit, rows[:n])) / n))
    return out


def score_one(name: str, rows: list[dict], cov: dict, extras) -> None:
    hdr = "| Cut | n | MAE | within ±1.0 | within ±0.5 | bias | Pearson r | QWK |\n|---|---|---|---|---|---|---|---|"
    print(f"\n## {name}\n{hdr}")
    for nm, sel in (("Raw", "raw"), ("Slab", "slab"), ("Combined", None)):
        print(f"| {nm} " + _row([(r["pred"], r["grade"]) for r in rows if sel in (None, r["pool"])]))
    for pool, bands in (("raw", RAW_BANDS), ("slab", SLAB_BANDS)):
        print(f"\n{pool} by band\n" + hdr.replace("Cut", "True grade"))
        for b, (lo, hi) in enumerate(bands):
            print(f"| {lo}-{hi} " + _row([(r["pred"], r["grade"]) for r in rows if r["pool"] == pool and r["band"] == b]))
    m = [_hit(r) for r in rows]
    for label, k in extras:
        o = [_hit(r, k) for r in rows]
        print(f"paired vs {label} ({sum(o)}/150 within ±1.0): fixes {sum(a and not b for a, b in zip(m, o))}, "
              f"breaks {sum(b and not a for a, b in zip(m, o))}, both miss {sum(not a and not b for a, b in zip(m, o))}")
    print("selective coverage (within ±1.0 at 25/40/50/75/100% of listings, ranked by confidence):")
    for nm, sel in (("raw", "raw"), ("slab", "slab"), ("all", None)):
        c = _coverage([r for r in rows if sel in (None, r["pool"])])
        print(f"  {nm}: " + ", ".join(f"{f:.0%} (n={n}) {a:.0%}" for f, n, a in c))
    p = [r["pred"] for r in rows]
    print(f"pred spread min {min(p)} max {max(p)} stdev {statistics.pstdev(p):.2f} "
          f"(true {statistics.pstdev([r['grade'] for r in rows]):.2f}); distinct values {len(set(p))}")
    for lab, sel in (("condition-word hit >=1", lambda c: c["hits"] >= 1), ("no condition prose", lambda c: c["hits"] == 0)):
        for pool in ("raw", "slab", None):
            s2 = [r for r in rows if sel(cov[r["rid"]]) and pool in (None, r["pool"])]
            if s2:
                print(f"  {lab}, {pool or 'all'}: n={len(s2)} within ±1.0 {sum(map(_hit, s2)) / len(s2):.0%} "
                      f"MAE {statistics.mean(abs(r['pred'] - r['grade']) for r in s2):.2f}")
    for t in sorted({r["tier"] for r in rows}):
        s2 = [r for r in rows if r["tier"] == t]
        print(f"  tier {t}: n={len(s2)} within ±1.0 {sum(map(_hit, s2)) / len(s2):.0%}")


def cmd_score(wd: Path, hd: Path, d1013: Path) -> None:
    key = json.loads((wd / "key.json").read_text())
    haiku_photo = {r["product_id"]: r["pred"] for r in json.loads((hd / "bui-1006" / "joined.json").read_text())}
    b11 = json.loads((hd / "bui-1011" / "preds_l14.json").read_text())["preds"]["ordinal"]
    b13 = json.loads((d1013 / "preds_l14_both.json").read_text())["preds"]
    base = []
    for rid, c in key.items():
        assert b13[rid]["pid"] == c["product_id"]
        base.append({"rid": rid, "pool": c["pool"], "grade": c["grade"], "band": c["band"],
                     "hph": haiku_photo[c["product_id"]], "b11": b11[rid], "b13": b13[rid]["pred"]})
    random.Random(1015).shuffle(base)
    cov = json.loads((wd / "coverage.json").read_text())
    extras = [("Haiku-photo (BUI-1006)", "hph"), ("BUI-1011 L-14", "b11"), ("BUI-1013 both", "b13")]
    hp, bad = load_haiku(wd)
    if bad:
        print(f"HAIKU INCOMPLETE: batches {bad} failed validation")
    tier = {"high": 2, "medium": 1, "low": 0}
    hrows = [{**b, "pred": hp[b["rid"]]["pred"], "cv": tier[hp[b["rid"]]["conf"]], "tier": hp[b["rid"]]["conf"]}
             for b in base if b["rid"] in hp]
    print(f"Haiku text: {len(hrows)}/150 listings")
    if len(hrows) == 150:
        score_one("Haiku, listing text", hrows, cov, extras)
    jp = wd / "preds_jev.json"
    if jp.exists():
        jv = json.loads(jp.read_text())
        jrows = [{**b, "pred": jv[b["rid"]]["pred"], "cv": jv[b["rid"]]["conf"],
                  "tier": "conf>=0.5" if jv[b["rid"]]["conf"] >= 0.5 else "conf<0.5"} for b in base if b["rid"] in jv]
        print(f"\nJev text: {len(jrows)}/150 listings")
        if len(jrows) == 150:
            score_one("Jev, listing text (argmax band midpoint)", jrows, cov, extras)
            score_one("Jev, listing text (probability-weighted expectation)",
                      [{**r, "pred": jv[r["rid"]]["ev"]} for r in jrows], cov, extras)
            if len(hrows) == 150:
                h = {r["rid"]: r for r in hrows}
                a = [_hit(r) for r in jrows]
                b = [_hit(h[r["rid"]]) for r in jrows]
                print(f"\npaired Jev vs Haiku-text: Jev fixes {sum(x and not y for x, y in zip(a, b))}, "
                      f"breaks {sum(y and not x for x, y in zip(a, b))}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("step", choices=["prepare", "validate", "jev", "score"])
    ap.add_argument("--workdir", required=True, type=Path)
    ap.add_argument("--holdout", type=Path, default=Path.home() / "comic-grader-fixtures" / "bui-1006-holdout")
    ap.add_argument("--bui1013", type=Path, help="dir holding BUI-1013's preds_l14_both.json")
    a = ap.parse_args()
    if a.step == "prepare":
        cmd_prepare(a.workdir, a.holdout)
    elif a.step == "validate":
        cmd_validate(a.workdir)
    elif a.step == "jev":
        cmd_jev(a.workdir)
    else:
        cmd_score(a.workdir, a.holdout, a.bui1013)


if __name__ == "__main__":
    main()
