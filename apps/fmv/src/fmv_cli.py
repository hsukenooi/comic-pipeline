"""comic-fmv CLI — thin Click wrapper around fmv_runner.run()."""

from __future__ import annotations

import importlib.metadata
import os
import sys

import click

import fmv_runner


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
                   "recompute everything.")
@click.option("--grade-window", "grade_window", type=float, default=None,
              help="Max grade-window the comp pool may widen to (e.g. 2.5). "
                   "Default 2.0. Only changes how far widening reaches — it does "
                   "NOT bypass the one-sided/spread guards (a guarded book stays "
                   "flagged for manual pricing).")
@click.option("--quiet", is_flag=True, help="Suppress the human table on stdout.")
@click.option("--brief", is_flag=True,
              help="BUI-362: after the human table, print one compact JSON "
                   "object per row (item_id, comic_id, fmv_id, max_bid, "
                   "flag_reason, confidence) — the linkage fields /comic:buy "
                   "threads into the snipe step, without reading the full "
                   "--out file. Combine with --quiet for the JSON lines only.")
@click.option("--server-url", envvar=["COMICS_SERVER_URL", "GIXEN_SERVER_URL"], default=None,
              help="Comics server URL (reads COMICS_SERVER_URL, "
                   "falling back to the deprecated GIXEN_SERVER_URL).")
def cli(batch_path: str | None, out_path: str | None,
        max_age_days: float, force: bool, grade_window: float | None,
        quiet: bool, brief: bool, server_url: str | None) -> None:
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
    """
    # BUI-220: warn when the server URL was supplied only via the deprecated
    # GIXEN_SERVER_URL env (the canonical name is COMICS_SERVER_URL).
    if not os.environ.get("COMICS_SERVER_URL") and os.environ.get("GIXEN_SERVER_URL"):
        click.echo(
            "warning: GIXEN_SERVER_URL is deprecated; use COMICS_SERVER_URL",
            err=True,
        )
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
