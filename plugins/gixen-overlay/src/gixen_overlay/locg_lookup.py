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

BUI-788: fail-soft is not the same as fail-SILENT. Every one of those causes
used to collapse into one bare `None`, so a row Metron merely THROTTLED — a
row a re-run resolves fine — was reported exactly like a row Metron genuinely
cannot resolve. That understates what a re-run would recover, and it is how
BUI-773's 90-row `backfill-year` residual read as fully structural when 5 of
the 90 were false negatives. Two changes close it:

1. `resolve_year_and_locg` takes an optional `outcome` out-dict (the same
   out-param shape `db.upsert_comic`'s `skip_reason` already uses) and records
   a `reason` from `RESOLVE_REASONS` plus a `retryable` flag. The return type
   is unchanged, so every existing caller keeps working untouched.
2. The child's rate-limit sleep cap is capped BY THIS MODULE'S OWN subprocess
   budget (`LOCG_RATE_LIMIT_SLEEP_CAP_SECONDS`, passed as
   `METRON_RATE_LIMIT_MAX_SLEEP`). `locg.metron`'s own default cap is 60s —
   TWICE this module's 30s `LOCG_TIMEOUT_SECONDS` — so a throttled child was
   guaranteed to be killed mid-sleep, and a throttled row could never resolve
   no matter how many times the endpoint was re-run. Deriving the child's cap
   from the parent's budget makes that mismatch impossible by construction
   rather than by keeping two constants in two packages in sync by hand, and
   converts a silent 30s stall into a fast in-band "throttled" verdict. It is
   deliberately NOT a fix in the other direction: raising `LOCG_TIMEOUT_SECONDS`
   would only deepen the event-loop stall this endpoint already has.
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
#
# BUI-788: this is a HARD ceiling on purpose and must not be raised to "give
# throttled rows room". `POST /api/comics/backfill-year` calls this
# synchronously from an async handler on a single-process server, so every
# second here blocks the sync loop and every other request — at `limit=3` the
# health gate has already reported "server is not responding" for ~90s. The
# throttle problem is fixed by shrinking the CHILD's sleep to fit this budget
# (below), not by growing the budget.
LOCG_TIMEOUT_SECONDS = 30

# The rate-limit retry sleep the child CLI is allowed, handed to it as
# `METRON_RATE_LIMIT_MAX_SLEEP` (see `locg.metron._rate_limit_max_sleep`).
# Derived from the budget above rather than written as its own number, so the
# invariant that matters — the child can never sleep past the moment we kill
# it — holds by construction. A quarter of the budget: a retry has to leave
# room for the request that follows it (a cold `resolve_issue_by_membership`
# has been measured at 26s of pure HTTP on a wide series), and a sleep longer
# than we can wait out buys nothing — the child is killed and reports
# nothing, which is the exact failure BUI-788 exists to remove.
#
# The cost of waking early is one extra Metron request: the retry fires before
# `retry_after` elapsed, is throttled again, and the child reports `throttled`
# in band. That is a real charge against the 5,000/day budget (bounded at one
# per throttled row), paid to turn a blind 30s stall into a reportable
# verdict — and under the old 60s cap that retry never happened at all,
# because the child was killed before it woke.
LOCG_RATE_LIMIT_SLEEP_CAP_SECONDS = LOCG_TIMEOUT_SECONDS / 4

# A `reason` echoed by the child is used as a roll-up key upstream; cap its
# length so a malformed child can't inflate the endpoint's response.
_MAX_REASON_LEN = 64

# BUI-788 failure taxonomy. `throttled`/`timeout` mean "Metron did not answer
# yet" — a re-run can still resolve the row. `unresolvable` means "Metron
# answered, and the answer is no" — a re-run cannot. Everything else is
# infrastructure. `_run_locg` reports the child's own `reason` verbatim when
# it sends one, so this tuple is the union of what this module raises and what
# `locg.commands.RESOLVE_YEAR_REASONS` can send up.
REASON_THROTTLED = "throttled"
REASON_TIMEOUT = "timeout"
REASON_UNRESOLVABLE = "unresolvable"
REASON_CREDENTIALS = "credentials"
REASON_INVALID_INPUT = "invalid_input"
REASON_CLI_ERROR = "cli_error"
REASON_DISABLED = "disabled"
RESOLVE_REASONS = (
    REASON_THROTTLED,
    REASON_TIMEOUT,
    REASON_UNRESOLVABLE,
    REASON_CREDENTIALS,
    REASON_INVALID_INPUT,
    REASON_CLI_ERROR,
    REASON_DISABLED,
)

# Reasons a re-run genuinely cannot recover. Everything NOT listed here —
# including a `reason` string this module has never seen, from a newer `locg`
# than the deployed overlay — counts as retryable. That default is the safe
# direction: over-reporting "worth retrying" costs one wasted lookup, while
# under-reporting it is precisely the BUI-788 bug (a recoverable row filed
# forever under "Metron doesn't know this issue").
_TERMINAL_REASONS = frozenset(
    {REASON_UNRESOLVABLE, REASON_CREDENTIALS, REASON_INVALID_INPUT, REASON_DISABLED}
)


def is_retryable_reason(reason: str | None) -> bool:
    """Whether a later re-run could still resolve a row that failed for `reason`."""
    return reason not in _TERMINAL_REASONS


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


def resolve_year_and_locg(
    series: str,
    issue: str,
    *,
    outcome: dict[str, object] | None = None,
) -> LocgResolution | None:
    """Resolve (series, issue) -> year via Metron (BUI-719).

    Returns None on any failure — the caller is expected to treat None as
    "skip this bid" with an informative reason.

    BUI-788: pass a dict as `outcome` to also learn WHY a None came back. On
    failure it is populated with ``reason`` (one of :data:`RESOLVE_REASONS`)
    and ``retryable`` (bool). It is left untouched on success and is never
    read, only written — so an existing caller that ignores it (and the
    `extract-comics` call site, which does) behaves exactly as before. Keyword-
    only so it can never be mistaken for a third lookup key.
    """
    def _fail(reason: str) -> LocgResolution | None:
        """Record why, then hand back the same None every caller already gets."""
        if outcome is not None:
            outcome["reason"] = reason
            outcome["retryable"] = is_retryable_reason(reason)
        return None

    if os.environ.get("LOCG_FALLBACK_DISABLED") == "1":
        return _fail(REASON_DISABLED)
    if not series or not issue:
        return _fail(REASON_INVALID_INPUT)

    # `--` forces argparse to treat `series`/`issue` as plain positionals no
    # matter their content — without it, an issue token that starts with a
    # dash and isn't a bare negative number (e.g. a "-1AU"-style one-shot
    # label) is misparsed as an unrecognized flag (exit 2, silently folded
    # into the fail-soft None below) rather than actually reaching Metron.
    run_outcome: dict[str, object] = {}
    result = _run_locg(["resolve-year", "--", series, issue], outcome=run_outcome)
    if not isinstance(result, dict):
        return _fail(str(run_outcome.get("reason") or REASON_CLI_ERROR))
    if result.get("error"):
        logger.info(
            "locg resolve-year error for %r #%s: %s", series, issue, result.get("error"),
        )
        # The child classifies its own failure (locg.commands's
        # RESOLVE_YEAR_REASONS). Trust its `reason` when it sends one and pass
        # it through verbatim — including a value this overlay predates. An
        # older `locg` that sends no `reason` at all is the one case we must
        # guess, and guessing UNRESOLVABLE there would recreate the exact bug
        # this ticket fixes, so an unlabelled child error is treated as
        # retryable `cli_error` instead.
        child_reason = result.get("reason")
        if isinstance(child_reason, str) and child_reason:
            # Truncated: this string becomes a JSON key in the endpoint's
            # `unresolved_by_reason` roll-up, so a malformed child must not be
            # able to bloat a response through it.
            return _fail(child_reason[:_MAX_REASON_LEN])
        return _fail(REASON_CLI_ERROR)

    year = result.get("year")
    if not isinstance(year, int):
        # A well-formed success envelope with no usable year: the child said
        # "ok" but gave us nothing to write. Not a throttle, not a Metron "no"
        # — a malformed response.
        return _fail(REASON_CLI_ERROR)

    return LocgResolution(year=year)


def _child_env() -> dict[str, str]:
    """The child CLI's environment: ours, plus the deadline-derived sleep cap.

    Copies `os.environ` and never mutates it. Both halves matter. Copying is
    what keeps the child's credentials, `LOCG_DATA_DIR` and PATH intact;
    NOT mutating is what keeps this cap scoped to the child, because the
    comics server runs Metron IN-PROCESS too (record-win batches, the audit
    sweeps) and those callers must keep the full 60s cap they were tuned for.

    An operator value in `~/.comics-server/.env` is honored but CLAMPED to our
    own budget rather than taken verbatim: it can only ever shorten the wait.
    A longer one cannot be waited out — we kill the child at
    `LOCG_TIMEOUT_SECONDS` regardless — so accepting it would quietly restore
    the exact BUI-788 defect from a config file, which is the hardest place to
    notice it.
    """
    env = dict(os.environ)
    cap = LOCG_RATE_LIMIT_SLEEP_CAP_SECONDS
    operator_value = env.get("METRON_RATE_LIMIT_MAX_SLEEP")
    if operator_value is not None:
        try:
            parsed = float(operator_value)
        except (TypeError, ValueError):
            parsed = 0.0
        if parsed > 0:
            cap = min(parsed, cap)
        else:
            # Unparseable or non-positive. Passing it through would make
            # `locg.metron._rate_limit_max_sleep` reject it and fall back to
            # ITS 60s default — over our budget, i.e. straight back to the
            # BUI-788 defect. Substitute our own cap instead.
            logger.warning(
                "Ignoring unusable METRON_RATE_LIMIT_MAX_SLEEP=%r; "
                "using the %.1fs budget-derived cap.", operator_value, cap,
            )
    env["METRON_RATE_LIMIT_MAX_SLEEP"] = str(cap)
    return env


def _run_locg(args: list[str], *, outcome: dict[str, object] | None = None) -> object | None:
    """Run `locg <args>` and return parsed JSON, or None on any failure.

    BUI-788: on failure, records a `reason` in `outcome` (when given) so the
    caller can tell a subprocess TIMEOUT — the child was still working when we
    killed it, which says nothing about whether the row is resolvable — apart
    from a child that ran to completion and reported a verdict.
    """
    def _fail(reason: str) -> object | None:
        if outcome is not None:
            outcome["reason"] = reason
        return None

    try:
        proc = subprocess.run(
            [LOCG_CMD, *args],
            capture_output=True,
            text=True,
            timeout=LOCG_TIMEOUT_SECONDS,
            check=False,
            env=_child_env(),
        )
    except subprocess.TimeoutExpired as e:
        logger.warning(
            "locg %s exceeded the %ss budget and was killed; the row is NOT "
            "known-unresolvable: %s", args, LOCG_TIMEOUT_SECONDS, e,
        )
        return _fail(REASON_TIMEOUT)
    except FileNotFoundError as e:
        logger.info("locg %s failed: %s", args, e)
        return _fail(REASON_CLI_ERROR)
    if proc.returncode != 0:
        logger.info("locg %s exited %d: %s", args, proc.returncode, proc.stderr.strip())
        return _fail(REASON_CLI_ERROR)
    out = proc.stdout.strip()
    if not out:
        return _fail(REASON_CLI_ERROR)
    try:
        return json.loads(out)
    except json.JSONDecodeError as e:
        logger.info("locg %s returned non-JSON: %s", args, e)
        return _fail(REASON_CLI_ERROR)
