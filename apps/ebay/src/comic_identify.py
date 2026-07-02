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

from comic_identity import identify_comic


def identity_to_dict(identity) -> dict:
    """Convert a ComicIdentity to a JSON-serializable dict.

    Drops _title_norm — the grade-stripped/normalized-title cache that exists
    purely so score_against_wish doesn't recompute it per wish item. It's an
    internal implementation detail, not part of the public identity contract
    a caller of this CLI should depend on.
    """
    data = dataclasses.asdict(identity)
    data.pop("_title_norm", None)
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
    args = parser.parse_args(argv)

    title = args.title
    if title is None:
        title = sys.stdin.read().strip()

    identity = identify_comic(title)
    print(json.dumps(identity_to_dict(identity)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
