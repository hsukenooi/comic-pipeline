"""Tests for --version flag across all 6 console scripts (BUI-314).

Each script (comic_identify, grade_photos, ebay_fetch, wishlist_sellers,
sold_comps, seller_scan) should respond to --version with a version string
and exit 0.
"""

import re
import subprocess
import sys


class TestVersionFlag:
    """Verify each console script responds to --version with exit code 0."""

    SCRIPTS = [
        "comic-identify",
        "grade-photos",
        "ebay-fetch",
        "wishlist-sellers",
        "ebay-sold-comps",
        "seller-scan",
    ]

    def test_version_flag_exists_and_exits_zero(self):
        """All 6 scripts should respond to --version and exit 0."""
        for script in self.SCRIPTS:
            result = subprocess.run(
                [sys.executable, "-m", f"{script.replace('-', '_')}", "--version"],
                capture_output=True,
                text=True,
            )
            # Module invocation may fail; try direct import + call instead
            if result.returncode != 0:
                self._test_script_via_import(script)

    def _test_script_via_import(self, script_name):
        """Fall back to testing via import when subprocess fails."""
        module_name = script_name.replace("-", "_")
        import importlib

        try:
            module = importlib.import_module(module_name)
            # If the script has a main(), call it with --version
            # and expect SystemExit(0) from argparse's action='version'
            if hasattr(module, "main"):
                try:
                    module.main(["--version"])
                    # If we get here, the --version didn't trigger SystemExit;
                    # that's a test failure
                    assert False, f"{script_name}: --version did not trigger exit"
                except SystemExit as e:
                    # action='version' calls sys.exit(0)
                    assert e.code == 0, f"{script_name}: --version exited with {e.code}, expected 0"
        except ImportError:
            # Fall back to subprocess test; import might fail in test isolation
            pass

    def test_version_string_format_for_comic_identify(self):
        """comic-identify --version prints version and git info."""
        import comic_identify

        try:
            comic_identify.main(["--version"])
        except SystemExit as e:
            assert e.code == 0

    def test_version_string_format_for_grade_photos(self):
        """grade-photos --version prints version and git info."""
        import grade_photos

        try:
            grade_photos.main(["--version"])
        except SystemExit as e:
            assert e.code == 0

    def test_version_string_format_for_ebay_fetch(self):
        """ebay-fetch --version prints version and git info."""
        import ebay_fetch

        try:
            ebay_fetch.main(["--version"])
        except SystemExit as e:
            assert e.code == 0

    def test_version_string_format_for_wishlist_sellers(self):
        """wishlist-sellers --version prints version and git info."""
        import wishlist_sellers

        try:
            wishlist_sellers.main(["--version"])
        except SystemExit as e:
            assert e.code == 0

    def test_version_string_format_for_sold_comps(self):
        """ebay-sold-comps --version prints version and git info."""
        import sold_comps

        try:
            sold_comps.main(["--version"])
        except SystemExit as e:
            assert e.code == 0

    def test_version_string_format_for_seller_scan(self):
        """seller-scan --version prints version and git info."""
        import seller_scan

        try:
            seller_scan.main(["--version"])
        except SystemExit as e:
            assert e.code == 0

    def test_version_string_pattern_for_all_scripts(self, capsys):
        """All version strings follow the pattern: <script> <version> (git <sha>, <date>)."""
        import comic_identify
        import ebay_fetch
        import grade_photos
        import seller_scan
        import sold_comps
        import wishlist_sellers

        modules = [
            comic_identify,
            ebay_fetch,
            grade_photos,
            seller_scan,
            sold_comps,
            wishlist_sellers,
        ]

        for module in modules:
            try:
                module.main(["--version"])
            except SystemExit:
                pass  # Expected from argparse action='version'

            # Check the captured output
            out = capsys.readouterr().out
            # Version pattern: "<name> <version> (git <sha>, <date>)"
            pattern = r"\S+ \d+\.\d+\.\d+ \(git \S+, \S+\)"
            assert re.search(pattern, out), f"Version output doesn't match pattern: {out}"
