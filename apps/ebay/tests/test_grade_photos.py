"""Tests for grade-photos CLI (BUI-283: consolidated onto ebay_fetch.py)."""

import json
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import requests

import ebay_fetch
import grade_photos


def _ok_response(content=b"fake-image-bytes"):
    """A requests.Response stand-in for a successful image download."""
    resp = MagicMock()
    resp.raise_for_status.return_value = None
    resp.content = content
    return resp


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
            with patch("grade_photos._download_image"):
                result = grade_photos.download_listing(
                    "fake-token", "123", tmp_path / "comic-1", ebay_fetch.PRODUCTION_BASE,
                )
        assert result["title"] == "Amazing Spider-Man #300 (NM-)"
        assert result["image_count"] == 3
        assert result["current_price"] == 12.34
        assert result["bid_count"] == 3

    def test_multi_image_download_calls_download_image_per_image(self, tmp_path):
        outdir = tmp_path / "comic-1"
        with patch("grade_photos.fetch_item", return_value=self._fake_item()):
            with patch("grade_photos._download_image") as mock_download:
                grade_photos.download_listing(
                    "fake-token", "123", outdir, ebay_fetch.PRODUCTION_BASE,
                )
        assert mock_download.call_count == 3
        first_call_args = mock_download.call_args_list[0][0]
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
            with patch("grade_photos._download_image"):
                result = grade_photos.download_listing(
                    "fake-token", "123", tmp_path / "comic-1", ebay_fetch.PRODUCTION_BASE,
                )
        assert result["current_price"] == 49.99
        assert result["bid_count"] is None

    def test_no_price_field_at_all_is_none(self, tmp_path):
        item = self._fake_item()
        del item["currentBidPrice"]
        with patch("grade_photos.fetch_item", return_value=item):
            with patch("grade_photos._download_image"):
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
# BUI-300: malformed Browse-API responses fail only that item
# ============================================================


class TestMalformedImageData:
    def test_missing_main_image_url_raises_runtime_error(self, tmp_path):
        """A malformed 'image' object missing 'imageUrl' used to raise a bare
        KeyError that would abort the whole batch. It must instead flow
        through the same RuntimeError/FETCH FAILED contract as a fetch
        failure."""
        item = {
            "title": "Malformed Listing",
            "image": {},  # no imageUrl key
            "currentBidPrice": {"value": "1.00"},
            "bidCount": 1,
        }
        with patch("grade_photos.fetch_item", return_value=item):
            try:
                grade_photos.download_listing(
                    "fake-token", "555", tmp_path / "comic-1", ebay_fetch.PRODUCTION_BASE,
                )
                assert False, "expected RuntimeError"
            except RuntimeError as e:
                assert "555" in str(e)

    def test_missing_additional_image_url_raises_runtime_error(self, tmp_path):
        """Same contract for a malformed entry inside additionalImages."""
        item = {
            "title": "Malformed Listing",
            "image": {"imageUrl": "https://example.com/main.jpg"},
            "additionalImages": [{"notImageUrl": "oops"}],
            "currentBidPrice": {"value": "1.00"},
            "bidCount": 1,
        }
        with patch("grade_photos.fetch_item", return_value=item):
            with patch("grade_photos._download_image"):
                try:
                    grade_photos.download_listing(
                        "fake-token", "556", tmp_path / "comic-1", ebay_fetch.PRODUCTION_BASE,
                    )
                    assert False, "expected RuntimeError"
                except RuntimeError as e:
                    assert "556" in str(e)

    def test_malformed_item_surfaces_as_fetch_failed_line_in_main(self, tmp_path, capsys):
        """End-to-end through main(): a malformed item must print the
        FETCH FAILED line and NOT abort the rest of the batch (BUI-300)."""
        good_item = {
            "title": "Fantastic Four #52",
            "image": {"imageUrl": "https://example.com/main.jpg"},
            "additionalImages": [],
            "currentBidPrice": {"value": "5.00"},
            "bidCount": 1,
        }
        bad_item = {"title": "Broken", "image": {}}
        with patch("grade_photos.load_config", return_value=("id", "secret", ebay_fetch.PRODUCTION_BASE)):
            with patch("grade_photos.get_token", return_value="fake-token"):
                with patch("grade_photos.fetch_item", side_effect=[good_item, bad_item, good_item]):
                    with patch("grade_photos._download_image"):
                        grade_photos.main(["111", "222", "333", "--workdir", str(tmp_path)])
        lines = capsys.readouterr().out.strip().splitlines()
        assert lines[0].startswith("comic-1: Fantastic Four #52")
        assert lines[1].startswith("comic-2: FETCH FAILED — ")
        assert lines[2].startswith("comic-3: Fantastic Four #52")


# ============================================================
# BUI-300: a hung image host fails only that item, not the batch
# ============================================================


class TestDownloadTimeout:
    def test_download_uses_bounded_timeout(self, tmp_path):
        """Every image download must pass a bounded timeout to requests.get —
        a hung image host must not stall the sequential batch indefinitely."""
        with patch("grade_photos.fetch_item", return_value=self._single_image_item()):
            with patch("grade_photos.requests.get", return_value=_ok_response()) as mock_get:
                grade_photos.download_listing(
                    "fake-token", "123", tmp_path / "comic-1", ebay_fetch.PRODUCTION_BASE,
                )
        assert mock_get.call_args.kwargs["timeout"] == grade_photos._DOWNLOAD_TIMEOUT_SECONDS

    def test_hung_host_raises_runtime_error_not_batch_abort(self, tmp_path):
        """A download that times out (requests.exceptions.Timeout) must fail
        only this item via RuntimeError/FETCH FAILED — not stall or crash the
        whole sequential batch."""
        with patch("grade_photos.fetch_item", return_value=self._single_image_item()):
            with patch("grade_photos.requests.get", side_effect=requests.exceptions.Timeout("timed out")):
                try:
                    grade_photos.download_listing(
                        "fake-token", "777", tmp_path / "comic-1", ebay_fetch.PRODUCTION_BASE,
                    )
                    assert False, "expected RuntimeError"
                except RuntimeError as e:
                    assert "777" in str(e)

    def test_http_error_status_raises_runtime_error(self, tmp_path):
        """A reachable host that responds with an error status (e.g. 404/500
        on the image URL itself) must also fail only this item."""
        error_response = MagicMock()
        error_response.raise_for_status.side_effect = requests.exceptions.HTTPError("404 Client Error")
        with patch("grade_photos.fetch_item", return_value=self._single_image_item()):
            with patch("grade_photos.requests.get", return_value=error_response):
                try:
                    grade_photos.download_listing(
                        "fake-token", "888", tmp_path / "comic-1", ebay_fetch.PRODUCTION_BASE,
                    )
                    assert False, "expected RuntimeError"
                except RuntimeError as e:
                    assert "888" in str(e)

    def test_hung_host_surfaces_as_fetch_failed_line_in_main_and_batch_continues(self, tmp_path, capsys):
        good_item = self._single_image_item()
        with patch("grade_photos.load_config", return_value=("id", "secret", ebay_fetch.PRODUCTION_BASE)):
            with patch("grade_photos.get_token", return_value="fake-token"):
                with patch("grade_photos.fetch_item", return_value=good_item):
                    with patch(
                        "grade_photos.requests.get",
                        side_effect=[requests.exceptions.Timeout("timed out"), _ok_response()],
                    ):
                        grade_photos.main(["111", "222", "--workdir", str(tmp_path)])
        lines = capsys.readouterr().out.strip().splitlines()
        assert lines[0].startswith("comic-1: FETCH FAILED — ")
        assert lines[1].startswith("comic-2: Fantastic Four #52")

    @staticmethod
    def _single_image_item():
        return {
            "title": "Fantastic Four #52",
            "image": {"imageUrl": "https://example.com/main.jpg"},
            "additionalImages": [],
            "currentBidPrice": {"value": "5.00"},
            "bidCount": 1,
        }


# ============================================================
# BUI-300: stale img-NN.jpg files from a prior run must not leak
# ============================================================


class TestStaleFileCleanup:
    def test_fewer_images_on_rerun_clears_stale_higher_numbered_files(self, tmp_path):
        """A re-run of the same label with FEWER images than a prior attempt
        must not leave the old higher-numbered img-NN.jpg files behind —
        mkdir(exist_ok=True) alone never clears them, so the grader could
        pick up a stale image from a previous listing."""
        outdir = tmp_path / "comic-1"
        outdir.mkdir(parents=True)
        (outdir / "img-01.jpg").write_bytes(b"old-1")
        (outdir / "img-02.jpg").write_bytes(b"old-2")
        (outdir / "img-03.jpg").write_bytes(b"old-3")

        one_image_item = {
            "title": "New Listing",
            "image": {"imageUrl": "https://example.com/main.jpg"},
            "additionalImages": [],
            "currentBidPrice": {"value": "1.00"},
            "bidCount": 1,
        }

        with patch("grade_photos.fetch_item", return_value=one_image_item):
            with patch("grade_photos.requests.get", return_value=_ok_response(b"new-1")):
                result = grade_photos.download_listing(
                    "fake-token", "123", outdir, ebay_fetch.PRODUCTION_BASE,
                )
        assert result["image_count"] == 1
        assert (outdir / "img-01.jpg").read_bytes() == b"new-1"
        assert not (outdir / "img-02.jpg").exists()
        assert not (outdir / "img-03.jpg").exists()

    def test_stale_file_from_unrelated_prior_listing_is_not_reused(self, tmp_path):
        """Guards the exact BUI-300 scenario: outdir pre-populated with a file
        that isn't even named like an img-NN.jpg (e.g. debris from a
        different run) must also be gone after download_listing runs."""
        outdir = tmp_path / "comic-1"
        outdir.mkdir(parents=True)
        (outdir / "img-01.jpg").write_bytes(b"stale-cover")

        with patch("grade_photos.fetch_item", return_value=None):
            try:
                grade_photos.download_listing(
                    "fake-token", "999", outdir, ebay_fetch.PRODUCTION_BASE,
                )
            except RuntimeError:
                pass
        # Even on a fetch failure, the stale file must not survive — a
        # subsequent successful retry into the same dir must start clean.
        assert not (outdir / "img-01.jpg").exists()


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
                    with patch("grade_photos._download_image"):
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
                    with patch("grade_photos._download_image"):
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
                    with patch("grade_photos._download_image"):
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

        # _download_image is mocked separately (rather than left to hit the
        # patched requests.get above) because ebay_fetch and grade_photos
        # both `import requests` — patching one module's `requests.get`
        # patches the single shared module attribute both see, so a real
        # image-download call here would consume a side_effect meant for
        # the item-lookup retry sequence.
        with patch("ebay_fetch.requests.get", side_effect=[rate_limited, ok]):
            with patch("ebay_fetch.time.sleep"):
                with patch("grade_photos._download_image"):
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
