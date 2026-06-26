"""comic-fmv CLI — thin Click wrapper around fmv_runner.run()."""

from __future__ import annotations

import os
import sys

import click

import fmv_runner


@click.command("comic-fmv")
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
@click.option("--server-url", envvar=["COMICS_SERVER_URL", "GIXEN_SERVER_URL"], default=None,
              help="Comics server URL (reads COMICS_SERVER_URL, "
                   "falling back to the deprecated GIXEN_SERVER_URL).")
def cli(batch_path: str | None, out_path: str | None,
        max_age_days: float, force: bool, grade_window: float | None,
        quiet: bool, server_url: str | None) -> None:
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
        server_url=server_url,
    )


def main() -> None:
    cli()


if __name__ == "__main__":
    main()
