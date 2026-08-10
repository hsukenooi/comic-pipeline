"""Metron-backed year fallback for eBay titles that omit a year (BUI-719).

`title_parser.parse_title()` requires a year, but real eBay listings often
don't include one. Rather than skipping such bids during `extract-comics`,
shell out to the `locg` CLI's `resolve-year` subcommand, which resolves
(series, issue) -> year via the Metron API.

BUI-719: this module used to shell out to `locg lookup <spec> --no-collection`
(a live LOCG site search) plus `locg comic <id>` on every cache miss. LOCG
blocks ALL programmatic/agent access now — a permanent, standing state
confirmed 2026-08-10, not a transient outage to route around, and not a
credentials problem (`locg login` itself is blocked). That made the old path
dead in practice on every cache miss, silently resolving 0/N. The resolution
SOURCE moved to Metron (the same `locg-cli` Metron client that already backs
`creator-run`/record-win), reached through the SAME subprocess boundary this
module has always used — only the CLI subcommand target changed (`locg
resolve-year <series> <issue>`, one call, instead of `locg lookup` + `locg
comic`, two).

Consequence: `locg_id`/`locg_variant_id` are now ALWAYS `None` on a
successful resolution — Metron has no notion of a LOCG comic id, only a
comic's own identity (title/issue/year). `db.upsert_comic` COALESCEs a
`None` `locg_id`/`locg_variant_id` against whatever is already stored for
that comic row (see its docstring), so this never regresses an existing LOCG
linkage from an earlier import — it just never adds a NEW one via this path
anymore. `LocgResolution` and `resolve_year_and_locg` keep their names for
caller compatibility (`routes.py`, tests) even though the source is no
longer LOCG at all; only `.year` is ever populated now.

This module is intentionally subprocess-based: gixen-overlay declares no
dependencies, so importing `locg` directly would pull a real Python package
into the plugin's install footprint. The CLI is the existing integration
boundary (every comic skill calls it via shell), and subprocess is easy to
mock in tests.

Behaviour is fail-soft: on any error (CLI missing, network failure, an
ambiguous Metron match — including a series-name match that doesn't actually
contain the requested issue number, or two that both do — or a resolved
issue with no usable date) the resolver returns None so that `extract-comics`
falls through to its existing skip path with a clearer reason. A wrong year
is worse than no year: year feeds the comics identity key (`idx_comics_tiyv`)
and vintage gates, so ambiguity is never resolved by guessing — see
`locg.metron.MetronClient.resolve_issue_by_membership`'s docstring for how
ambiguity is detected. Set ``LOCG_FALLBACK_DISABLED=1`` to short-circuit the
resolver.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
from dataclasses import dataclass

logger = logging.getLogger(__name__)


# Resolved at module load so tests can monkeypatch the constant if needed.
# The CLI installs as `locg` on PATH; PYTHONPATH/site-packages handle the
# import side.
LOCG_CMD = os.environ.get("LOCG_CMD", "locg")

# Bounded so a hung CLI invocation can't stall `extract-comics`.
LOCG_TIMEOUT_SECONDS = 30


@dataclass(frozen=True)
class LocgResolution:
    """Result of a successful year resolution.

    `locg_id`/`locg_variant_id` are always ``None`` as of BUI-719 (see the
    module docstring — the resolution source is Metron, which has no LOCG id
    mapping). Kept on the dataclass, defaulted to ``None``, only so callers
    that still read them (`upsert_comic`'s COALESCE-based linkage-preserving
    write) don't need to change.
    """
    year: int
    locg_id: int | None = None
    locg_variant_id: int | None = None


def resolve_year_and_locg(series: str, issue: str) -> LocgResolution | None:
    """Resolve (series, issue) -> year via Metron (BUI-719).

    Returns None on any failure — the caller is expected to treat None as
    "skip this bid" with an informative reason.
    """
    if os.environ.get("LOCG_FALLBACK_DISABLED") == "1":
        return None
    if not series or not issue:
        return None

    # `--` forces argparse to treat `series`/`issue` as plain positionals no
    # matter their content — without it, an issue token that starts with a
    # dash and isn't a bare negative number (e.g. a "-1AU"-style one-shot
    # label) is misparsed as an unrecognized flag (exit 2, silently folded
    # into the fail-soft None below) rather than actually reaching Metron.
    result = _run_locg(["resolve-year", "--", series, issue])
    if not isinstance(result, dict):
        return None
    if result.get("error"):
        logger.info(
            "locg resolve-year error for %r #%s: %s", series, issue, result.get("error"),
        )
        return None

    year = result.get("year")
    if not isinstance(year, int):
        return None

    return LocgResolution(year=year)


def _run_locg(args: list[str]) -> object | None:
    """Run `locg <args>` and return parsed JSON, or None on any failure."""
    try:
        proc = subprocess.run(
            [LOCG_CMD, *args],
            capture_output=True,
            text=True,
            timeout=LOCG_TIMEOUT_SECONDS,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        logger.info("locg %s failed: %s", args, e)
        return None
    if proc.returncode != 0:
        logger.info("locg %s exited %d: %s", args, proc.returncode, proc.stderr.strip())
        return None
    out = proc.stdout.strip()
    if not out:
        return None
    try:
        return json.loads(out)
    except json.JSONDecodeError as e:
        logger.info("locg %s returned non-JSON: %s", args, e)
        return None
