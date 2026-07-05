#!/usr/bin/env python3
"""comic-identify: freeform eBay listing title -> ComicIdentity, as JSON.

Thin CLI wrapper around comic_identity.identify_comic() (BUI-253 Step 3) so
the /comic:* skills and any LLM agent can shell out to ONE canonical
title-parsing implementation — series/issue/year/volume/edition/lot
detection — instead of re-deriving those rules in prose. Mirrors the other
apps/ebay console scripts (seller-scan, ebay-sold-comps): a thin argparse
wrapper around a library function, JSON out.
"""

import argparse
import dataclasses
import json
import sys

from comic_identity import extract_variant_text, identify_comic


def identity_to_dict(identity) -> dict:
    """Convert a ComicIdentity to a JSON-serializable dict.

    Drops _title_norm — the grade-stripped/normalized-title cache that exists
    purely so score_against_wish doesn't recompute it per wish item. It's an
    internal implementation detail, not part of the public identity contract
    a caller of this CLI should depend on.

    Adds variant_text (BUI-295): a short canonical distribution-variant label
    (Newsstand / Direct Edition / Whitman, or "" when none) derived from the
    title, so /comic:collection-add can read it straight into identify_data
    instead of re-deriving it with ad-hoc regex. This is a CLI-output field, not
    a ComicIdentity dataclass field — the library contract stays untouched.
    """
    data = dataclasses.asdict(identity)
    data.pop("_title_norm", None)
    data["variant_text"] = extract_variant_text(identity.title)
    return data


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="comic-identify",
        description=(
            "Extract a best-effort ComicIdentity (series, issue, year, "
            "volume, edition, lot detection + expansion, reject reasons, "
            "confidence) from a freeform eBay listing title."
        ),
    )
    parser.add_argument(
        "title",
        nargs="?",
        default=None,
        help="The listing title to identify. Reads from stdin if omitted "
             "(so it can sit at the end of a shell pipeline).",
    )
    parser.add_argument(
        "--batch",
        action="store_true",
        help="Batch mode: read newline-delimited titles from stdin and emit "
             "EXACTLY one JSON object per input line (JSONL), in order. The 1:1 "
             "line correspondence lets a caller map results back positionally — "
             "a blank line yields a null-series row (never dropped, so alignment "
             "holds), and a title that fails to parse yields an {\"error\": ...} "
             "row rather than aborting the batch. Lets a caller identify many "
             "titles in one invocation instead of one process per title.",
    )
    args = parser.parse_args(argv)

    if args.batch:
        # Emit exactly one line per input line so a caller can map results
        # positionally (see --batch help). Never skip and never abort mid-stream:
        # a blank line becomes identify_comic("") (null series — the caller's
        # own "series/issue blank -> ask" guard catches it), and an unexpected
        # raise becomes an error row instead of killing the whole batch.
        for line in sys.stdin:
            title = line.strip()
            try:
                identity = identify_comic(title)
                print(json.dumps(identity_to_dict(identity)))
            except Exception as exc:  # noqa: BLE001 — one bad title must not abort the batch
                print(json.dumps({"title": title, "series": None, "issue": None,
                                  "variant_text": "", "error": str(exc)}))
        return 0

    title = args.title
    if title is None:
        title = sys.stdin.read().strip()

    identity = identify_comic(title)
    print(json.dumps(identity_to_dict(identity)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
