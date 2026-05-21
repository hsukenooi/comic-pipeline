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
@click.option("--quiet", is_flag=True, help="Suppress the human table on stdout.")
@click.option("--server-url", envvar="GIXEN_SERVER_URL", default=None,
              help="Gixen server URL (also reads GIXEN_SERVER_URL).")
def cli(batch_path: str | None, out_path: str | None,
        max_age_days: float, force: bool, quiet: bool,
        server_url: str | None) -> None:
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
    fmv_runner.run(
        batch_path=batch_path,
        out_path=out_path,
        max_age_days=max_age_days,
        force=force,
        quiet=quiet,
        server_url=server_url,
    )


def main() -> None:
    cli()


if __name__ == "__main__":
    main()
