"""Tests for grade-photos CLI (BUI-283: consolidated onto ebay_fetch.py)."""

import json
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import ebay_fetch
import grade_photos


# ============================================================
# Import-time safety (BUI-283 fixed a latent import-time bug)
# ============================================================


class TestImportSafety:
    def test_import_does_not_touch_config(self, monkeypatch):
        """Importing the module must not read config or hit the network — no
        module-level credential loading. Reproduces the pre-BUI-283 bug where
        this module unconditionally opened ~/.config/ebay-fetch/config.json at
        import time and raised FileNotFoundError on an env-var-only machine."""
        script = (
            "import os\n"
            "os.environ.pop('EBAY_CLIENT_ID', None)\n"
            "os.environ.pop('EBAY_CLIENT_SECRET', None)\n"
            "import grade_photos\n"
            "print('OK')\n"
        )
        src_dir = str(Path(__file__).parent.parent / "src")
        result = subprocess.run(
            [sys.executable, "-c", script],
            cwd=src_dir,
            env={"PATH": "/usr/bin:/bin", "HOME": "/nonexistent-home"},
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr
        assert "OK" in result.stdout


# ============================================================
# download_listing — happy path, BIN fallback, FETCH FAILED
# ============================================================


class TestDownloadListing:
    def _fake_item(self, **overrides):
        data = {
            "title": "Amazing Spider-Man #300 (NM-)",
            "image": {"imageUrl": "https://example.com/main.jpg"},
            "additionalImages": [
                {"imageUrl": "https://example.com/1.jpg"},
                {"imageUrl": "https://example.com/2.jpg"},
            ],
            "currentBidPrice": {"value": "12.34"},
            "bidCount": 3,
        }
        data.update(overrides)
        return data

    def test_happy_path_title_images_price_bids(self, tmp_path):
        with patch("grade_photos.fetch_item", return_value=self._fake_item()):
            with patch("grade_photos.urllib.request.urlretrieve"):
                result = grade_photos.download_listing(
                    "fake-token", "123", tmp_path / "comic-1", ebay_fetch.PRODUCTION_BASE,
                )
        assert result["title"] == "Amazing Spider-Man #300 (NM-)"
        assert result["image_count"] == 3
        assert result["current_price"] == 12.34
        assert result["bid_count"] == 3

    def test_multi_image_download_calls_urlretrieve_per_image(self, tmp_path):
        outdir = tmp_path / "comic-1"
        with patch("grade_photos.fetch_item", return_value=self._fake_item()):
            with patch("grade_photos.urllib.request.urlretrieve") as mock_retrieve:
                grade_photos.download_listing(
                    "fake-token", "123", outdir, ebay_fetch.PRODUCTION_BASE,
                )
        assert mock_retrieve.call_count == 3
        first_call_args = mock_retrieve.call_args_list[0][0]
        assert first_call_args[0] == "https://example.com/main.jpg"
        assert first_call_args[1] == outdir / "img-01.jpg"

    def test_bin_price_fallback_no_current_bid_price(self, tmp_path):
        """BUI-165: a fixed-price (BIN) listing has no currentBidPrice, so
        current_price falls back to the `price` field and bid_count is None."""
        item = self._fake_item(
            currentBidPrice=None,
            price={"value": "49.99"},
            bidCount=None,
        )
        # currentBidPrice absent entirely (not just falsy) mirrors the real API shape.
        del item["currentBidPrice"]
        with patch("grade_photos.fetch_item", return_value=item):
            with patch("grade_photos.urllib.request.urlretrieve"):
                result = grade_photos.download_listing(
                    "fake-token", "123", tmp_path / "comic-1", ebay_fetch.PRODUCTION_BASE,
                )
        assert result["current_price"] == 49.99
        assert result["bid_count"] is None

    def test_no_price_field_at_all_is_none(self, tmp_path):
        item = self._fake_item()
        del item["currentBidPrice"]
        with patch("grade_photos.fetch_item", return_value=item):
            with patch("grade_photos.urllib.request.urlretrieve"):
                result = grade_photos.download_listing(
                    "fake-token", "123", tmp_path / "comic-1", ebay_fetch.PRODUCTION_BASE,
                )
        assert result["current_price"] is None

    def test_fetch_item_none_raises_runtime_error(self, tmp_path):
        """BUI-283 adapter: fetch_item() returning None (its failure contract)
        must surface as a raised RuntimeError so main() emits FETCH FAILED —
        never silently falling through to an image_count=0 result (BUI-147)."""
        with patch("grade_photos.fetch_item", return_value=None):
            try:
                grade_photos.download_listing(
                    "fake-token", "999", tmp_path / "comic-1", ebay_fetch.PRODUCTION_BASE,
                )
                assert False, "expected RuntimeError"
            except RuntimeError as e:
                assert "999" in str(e)


# ============================================================
# main() — stdout contract grade.md Step 1 depends on
# ============================================================


class TestMainStdoutContract:
    def _fake_item(self):
        return {
            "title": "Fantastic Four #52",
            "image": {"imageUrl": "https://example.com/main.jpg"},
            "additionalImages": [],
            "currentBidPrice": {"value": "5.00"},
            "bidCount": 1,
        }

    def test_happy_path_line_format(self, tmp_path, capsys):
        with patch("grade_photos.load_config", return_value=("id", "secret", ebay_fetch.PRODUCTION_BASE)):
            with patch("grade_photos.get_token", return_value="fake-token"):
                with patch("grade_photos.fetch_item", return_value=self._fake_item()):
                    with patch("grade_photos.urllib.request.urlretrieve"):
                        grade_photos.main(["123", "--workdir", str(tmp_path)])
        out = capsys.readouterr().out
        assert out.strip() == "comic-1: Fantastic Four #52 — 1 images — current bid $5.00 (1 bids)"

    def test_fetch_failed_line_format(self, tmp_path, capsys):
        """BUI-147: a fetch failure must print FETCH FAILED, never a
        0-images success line."""
        with patch("grade_photos.load_config", return_value=("id", "secret", ebay_fetch.PRODUCTION_BASE)):
            with patch("grade_photos.get_token", return_value="fake-token"):
                with patch("grade_photos.fetch_item", return_value=None):
                    grade_photos.main(["999", "--workdir", str(tmp_path)])
        out = capsys.readouterr().out.strip()
        assert out.startswith("comic-1: FETCH FAILED — ")
        assert "images" not in out

    def test_comic_n_label_numbering_multiple_items(self, tmp_path, capsys):
        with patch("grade_photos.load_config", return_value=("id", "secret", ebay_fetch.PRODUCTION_BASE)):
            with patch("grade_photos.get_token", return_value="fake-token"):
                with patch("grade_photos.fetch_item", side_effect=[self._fake_item(), None, self._fake_item()]):
                    with patch("grade_photos.urllib.request.urlretrieve"):
                        grade_photos.main(["111", "222", "333", "--workdir", str(tmp_path)])
        lines = capsys.readouterr().out.strip().splitlines()
        assert lines[0].startswith("comic-1: Fantastic Four #52")
        assert lines[1].startswith("comic-2: FETCH FAILED — ")
        assert lines[2].startswith("comic-3: Fantastic Four #52")

    def test_get_token_called_once_across_multiple_items(self, tmp_path):
        """The token is fetched once per run and reused across items — the
        per-item OAuth call this module used to make is gone."""
        with patch("grade_photos.load_config", return_value=("id", "secret", ebay_fetch.PRODUCTION_BASE)):
            with patch("grade_photos.get_token", return_value="fake-token") as mock_get_token:
                with patch("grade_photos.fetch_item", return_value=self._fake_item()):
                    with patch("grade_photos.urllib.request.urlretrieve"):
                        grade_photos.main(["111", "222", "--workdir", str(tmp_path)])
        assert mock_get_token.call_count == 1


# ============================================================
# End-to-end through the real ebay_fetch functions (mocking only
# requests) — proves the reuse actually routes through ebay_fetch's
# retry/caching behavior, not a shadowed copy.
# ============================================================


class TestEndToEndThroughEbayFetch:
    def test_429_retry_surfaces_through_fetch_item(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ebay_fetch, "CONFIG_DIR", tmp_path)

        rate_limited = MagicMock()
        rate_limited.status_code = 429

        ok = MagicMock()
        ok.status_code = 200
        ok.json.return_value = {
            "title": "Retry OK",
            "image": {"imageUrl": "https://example.com/main.jpg"},
            "currentBidPrice": {"value": "1.00"},
            "bidCount": 1,
        }

        with patch("ebay_fetch.requests.get", side_effect=[rate_limited, ok]):
            with patch("ebay_fetch.time.sleep"):
                with patch("grade_photos.urllib.request.urlretrieve"):
                    result = grade_photos.download_listing(
                        "fake-token", "123", tmp_path / "comic-1", ebay_fetch.PRODUCTION_BASE,
                    )
        assert result["title"] == "Retry OK"

    def test_get_token_caches_across_calls(self, tmp_path, monkeypatch):
        """BUI-283: adopting ebay_fetch's token cache means a second
        get_token() call within the 5-min buffer reuses the cached token
        instead of making a fresh OAuth request."""
        monkeypatch.setattr(ebay_fetch, "CONFIG_DIR", tmp_path)
        cache_file = tmp_path / "token_cache_production.json"
        cache_file.write_text(json.dumps({
            "access_token": "cached-token",
            "expires_at": time.time() + 3600,
        }))

        with patch("ebay_fetch.requests.post") as mock_post:
            token = grade_photos.get_token("id", "secret", ebay_fetch.PRODUCTION_BASE)
        assert token == "cached-token"
        mock_post.assert_not_called()
