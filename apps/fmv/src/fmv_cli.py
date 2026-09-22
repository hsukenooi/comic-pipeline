"""comic-fmv CLI — thin Click wrapper around fmv_runner.run() (plus the
read-only --inversion-sweep and --sentinel-probe report modes)."""

from __future__ import annotations

import importlib.metadata
import os
import sys

import click

import fmv_runner
import sentinel_probe as sentinel_probe_module


def _version_string() -> str:
    """BUI-305: staleness signal for a `uv tool install`ed binary.

    `_fmv_build_stamp` is generated at build time by hatch_build.py from the
    git HEAD of the source tree the wheel was built from; it's absent when
    running from an unbuilt checkout (e.g. `uv run` here in tests), so fall
    back to "unknown" rather than failing.
    """
    try:
        pkg_version = importlib.metadata.version("comic-fmv")
    except importlib.metadata.PackageNotFoundError:
        pkg_version = "unknown"
    try:
        from _fmv_build_stamp import GIT_DATE, GIT_SHA
    except ImportError:
        GIT_SHA, GIT_DATE = "unknown", "unknown"
    return f"comic-fmv {pkg_version} (git {GIT_SHA}, {GIT_DATE})"


def _print_version(ctx: click.Context, param: click.Parameter, value: bool) -> None:
    if not value or ctx.resilient_parsing:
        return
    click.echo(_version_string())
    ctx.exit()


@click.command("comic-fmv")
@click.option("--version", is_flag=True, expose_value=False, is_eager=True,
              callback=_print_version,
              help="Print the installed version and the git SHA/date it was built "
                   "from, then exit. Use this to check for a stale `uv tool install` "
                   "(see scripts/install.sh).")
@click.option("--batch", "batch_path", type=click.Path(exists=True),
              help="Path to JSON batch of books to value (or '-' for stdin).")
@click.option("--out", "out_path", type=click.Path(),
              help="Write structured JSON output to this path ('-' for stdout).")
@click.option("--max-age-days", type=float, default=7.0,
              help="Reuse FMVs already in the Gixen DB if fmv_updated_at is "
                   "within N days. Default 7.")
@click.option("--force", is_flag=True,
              help="Bypass both the SerpApi response cache and the DB FMV cache; "
                   "recompute everything. Without --force, a hand-priced row "
                   "(fmv.provenance='hand', or — for a row that predates that "
                   "column — fmv_notes OPENING with 'hand' or 'manual', any "
                   "case, e.g. 'hand §', 'manual:') is always "
                   "skipped, even if stale (BUI-533/759/769); --force overwrites it and "
                   "echoes the old notes to stderr. If the comics-server lookup "
                   "that answers 'is this hand-priced?' FAILS, the book is "
                   "skipped and left untouched instead — reported separately "
                   "from the hand-priced skips, and --force does not bypass it "
                   "(BUI-544).")
@click.option("--grade-window", "grade_window", type=float, default=None,
              help="Max grade-window the comp pool may widen to (e.g. 2.5). "
                   "Default 2.0. Only changes how far widening reaches — it does "
                   "NOT bypass the one-sided/spread guards (a guarded book stays "
                   "flagged for manual pricing).")
@click.option("--quiet", is_flag=True, help="Suppress the human table on stdout.")
@click.option("--brief", is_flag=True,
              help="BUI-362: after the human table, print one compact JSON "
                   "object per row (item_id, comic_id, fmv_id, max_bid, "
                   "fmv_low, fmv_high, fmv_notes, flag_reason, confidence, "
                   "source) on stdout — the linkage fields /comic:buy "
                   "threads into the snipe step, without reading the full "
                   "--out file. `source` (BUI-549) distinguishes a "
                   "comics-server lookup-error skip (skipped_lookup_error) "
                   "from an ordinary unpriced row. Combine with --quiet for "
                   "the JSON lines only.")
@click.option("--server-url", envvar=["COMICS_SERVER_URL", "GIXEN_SERVER_URL"], default=None,
              help="Comics server URL (reads COMICS_SERVER_URL, "
                   "falling back to the deprecated GIXEN_SERVER_URL).")
@click.option("--inversion-sweep", is_flag=True,
              help="BUI-583: report every cross-grade FMV inversion already in "
                   "the DB (same book, a higher grade priced below a lower "
                   "one) and exit without pricing anything. Reads existing "
                   "rows only — zero provider requests, so it is safe to run "
                   "any time. Advisory: nothing is written and no price "
                   "changes. Ignores --batch.")
@click.option("--sentinel-probe", is_flag=True,
              help="BUI-603: run the fixed sentinel + negative-control "
                   "calibration batch (2-3 deep-liquid keys + one query "
                   "guaranteed to match nothing) through ebay-sold-comps and "
                   "report pass/fail — n=0 or a wild price jump on a "
                   "sentinel, or ANY comps on the negative control, alerts. "
                   "Calibration only: nothing is ever upserted to the "
                   "fmv/comics DB. Spends real provider requests (unless "
                   "cache-fresh) — run weekly, not per-invocation. Ignores "
                   "--batch. Exits 0 (healthy), 1 (a check failed), or 2 "
                   "(the probe itself could not complete).")
@click.option("--list-slab-watch", is_flag=True,
              help="BUI-950: print the slab watch set — wish-list books "
                   "whose highest raw FMV clears SLAB_WATCH_MIN_FMV, plus "
                   "any hand override — from GET /api/comics/slab-watch, "
                   "then exit. Read-only: no comps are fetched, nothing is "
                   "written. Ignores --batch.")
@click.option("--slab-watch-collect", is_flag=True,
              help="BUI-951: for every book in the slab watch set "
                   "(GET /api/comics/slab-watch) whose comps ledger has no "
                   "slab observation in the last 21 days, fetch ONE "
                   "certifier-targeted query and POST the surviving comps "
                   "to the ledger (pool='slab') — never prices or upserts "
                   "an FMV. Meant for a scheduled (~4-weekly) launchd job "
                   "(scripts/launchd/com.comics.slab-watch-collect.plist), "
                   "not interactive use. Hard-capped at "
                   "SLAB_WATCH_MAX_REQUESTS provider requests per run "
                   "(default 80); leftover books roll to the next run. "
                   "Pings /api/heartbeat/slab-watch-collect only when every "
                   "fetch and every ledger write this run succeeded — a run "
                   "where every book was already fresh still pings (zero "
                   "results is a success). Exits 0 (success), 1 (a fetch or "
                   "write failed), 2 (could not start). Ignores --batch.")
def cli(batch_path: str | None, out_path: str | None,
        max_age_days: float, force: bool, grade_window: float | None,
        quiet: bool, brief: bool, server_url: str | None,
        inversion_sweep: bool, sentinel_probe: bool,
        list_slab_watch: bool, slab_watch_collect: bool) -> None:
    """Compute fair market value for a batch of comics.

    Pipeline per book:

    \b
      1. (skip-if-cached) GET /api/comics?locg_id=...&grade=...&max_age_days=N
         to reuse a recent DB FMV
      2. Shell out to `ebay-sold-comps` (apps/ebay) for any books still
         needing fresh comps
      3. Run IQR + quartiles + confidence rubric on the comp pool
      4. POST /api/comics to upsert the FMV (gixen-overlay stamps fmv_updated_at)

    Input batch JSON shape:

    \b
      [{"item_id": "...", "title": "...", "issue": "...", "year": 1984,
        "grade": 8.0, "locg_id": 1081721, "locg_variant_id": null,
        "publisher": "dark horse", "notes": "..."}, ...]

    \b
    BUI-930 — a CERTIFIED (slab) row adds four keys, all emitted by
    `ebay-fetch --identify`:
      certifier     "cgc" | "cbcs" | "other"  (absent/"none" == a raw book,
                    which is every existing batch, unchanged)
      label         "universal" | "signature_series" | "qualified" |
                    "restored" | "conserved"  (default "universal")
      page_quality  "white" | "ow_w" | "ow" | "c_ow" | "cream" | "unknown"
      listing_type  "Auction" | "BIN"  — the key `ebay_fetch.parse_item`
                    emits; "BIN" renders the band with NO max bid (R34).
    A row with a certifier is priced from that certifier's own slab sales at
    that label, never from the raw market, and `grade_confidence` is ignored
    for it (a certified grade is not a photo judgement).
    """
    # BUI-220: warn when the server URL was supplied only via the deprecated
    # GIXEN_SERVER_URL env (the canonical name is COMICS_SERVER_URL).
    if not os.environ.get("COMICS_SERVER_URL") and os.environ.get("GIXEN_SERVER_URL"):
        click.echo(
            "warning: GIXEN_SERVER_URL is deprecated; use COMICS_SERVER_URL",
            err=True,
        )
    # BUI-583: a read-only consistency report, not a pricing run — handled
    # before run()'s --batch gate, which would otherwise reject the sweep for
    # missing an input batch it does not use.
    if inversion_sweep:
        fmv_runner.run_inversion_sweep(server_url=server_url)
        return
    # BUI-603: a calibration report, not a pricing run — same reason as
    # --inversion-sweep above, handled before run()'s --batch/--server-url
    # gates (a sentinel probe needs neither: its batch is fixed, and the
    # heartbeat ping it attempts on success is best-effort/optional).
    if sentinel_probe:
        sys.exit(sentinel_probe_module.run_sentinel_probe(server_url=server_url))
    # BUI-950: same "handled before run()'s --batch gate" reason as
    # --inversion-sweep/--sentinel-probe above — a read-only report mode
    # with no batch input of its own.
    if list_slab_watch:
        fmv_runner.run_list_slab_watch(server_url=server_url)
        return
    # BUI-951: a scheduled collection run, not a pricing run — same
    # "handled before run()'s --batch gate" reason as the three modes above.
    # Unlike them it DOES write (the comps ledger), so — unlike
    # --list-slab-watch — it returns a real success/failure exit code rather
    # than always returning 0.
    if slab_watch_collect:
        sys.exit(fmv_runner.run_slab_watch_collect(server_url=server_url))
    fmv_runner.run(
        batch_path=batch_path,
        out_path=out_path,
        max_age_days=max_age_days,
        force=force,
        grade_window=grade_window,
        quiet=quiet,
        brief=brief,
        server_url=server_url,
    )


def main() -> None:
    cli()


if __name__ == "__main__":
    main()
