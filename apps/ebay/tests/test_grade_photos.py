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
        with patch("grade_photos.fetch_item_with_status", return_value=(self._fake_item(), 200)):
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
        with patch("grade_photos.fetch_item_with_status", return_value=(self._fake_item(), 200)):
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
        with patch("grade_photos.fetch_item_with_status", return_value=(item, 200)):
            with patch("grade_photos._download_image"):
                result = grade_photos.download_listing(
                    "fake-token", "123", tmp_path / "comic-1", ebay_fetch.PRODUCTION_BASE,
                )
        assert result["current_price"] == 49.99
        assert result["bid_count"] is None

    def test_no_price_field_at_all_is_none(self, tmp_path):
        item = self._fake_item()
        del item["currentBidPrice"]
        with patch("grade_photos.fetch_item_with_status", return_value=(item, 200)):
            with patch("grade_photos._download_image"):
                result = grade_photos.download_listing(
                    "fake-token", "123", tmp_path / "comic-1", ebay_fetch.PRODUCTION_BASE,
                )
        assert result["current_price"] is None

    def test_fetch_item_none_raises_runtime_error(self, tmp_path):
        """BUI-283 adapter: fetch_item_with_status() returning (None, status)
        for a non-401 failure must surface as a raised RuntimeError so main()
        emits FETCH FAILED — never silently falling through to an
        image_count=0 result (BUI-147)."""
        with patch("grade_photos.fetch_item_with_status", return_value=(None, 500)):
            try:
                grade_photos.download_listing(
                    "fake-token", "999", tmp_path / "comic-1", ebay_fetch.PRODUCTION_BASE,
                )
                assert False, "expected RuntimeError"
            except RuntimeError as e:
                assert "999" in str(e)

    def test_fetch_item_401_raises_token_expired_error(self, tmp_path):
        """BUI-310: a 401 must raise the more specific TokenExpiredError (a
        RuntimeError subclass) so main() can refresh the token and retry —
        distinct from the generic fetch-failure RuntimeError above."""
        with patch("grade_photos.fetch_item_with_status", return_value=(None, 401)):
            try:
                grade_photos.download_listing(
                    "fake-token", "999", tmp_path / "comic-1", ebay_fetch.PRODUCTION_BASE,
                )
                assert False, "expected TokenExpiredError"
            except grade_photos.TokenExpiredError as e:
                assert "999" in str(e)
                assert isinstance(e, RuntimeError)


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
        with patch("grade_photos.fetch_item_with_status", return_value=(item, 200)):
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
        with patch("grade_photos.fetch_item_with_status", return_value=(item, 200)):
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
                with patch(
                    "grade_photos.fetch_item_with_status",
                    side_effect=[(good_item, 200), (bad_item, 200), (good_item, 200)],
                ):
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
        with patch("grade_photos.fetch_item_with_status", return_value=(self._single_image_item(), 200)):
            with patch("grade_photos.requests.get", return_value=_ok_response()) as mock_get:
                grade_photos.download_listing(
                    "fake-token", "123", tmp_path / "comic-1", ebay_fetch.PRODUCTION_BASE,
                )
        assert mock_get.call_args.kwargs["timeout"] == grade_photos._DOWNLOAD_TIMEOUT_SECONDS

    def test_hung_host_raises_runtime_error_not_batch_abort(self, tmp_path):
        """A download that times out (requests.exceptions.Timeout) must fail
        only this item via RuntimeError/FETCH FAILED — not stall or crash the
        whole sequential batch."""
        with patch("grade_photos.fetch_item_with_status", return_value=(self._single_image_item(), 200)):
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
        with patch("grade_photos.fetch_item_with_status", return_value=(self._single_image_item(), 200)):
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
                with patch("grade_photos.fetch_item_with_status", return_value=(good_item, 200)):
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

        with patch("grade_photos.fetch_item_with_status", return_value=(one_image_item, 200)):
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

        with patch("grade_photos.fetch_item_with_status", return_value=(None, 500)):
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
                with patch("grade_photos.fetch_item_with_status", return_value=(self._fake_item(), 200)):
                    with patch("grade_photos._download_image"):
                        grade_photos.main(["123", "--workdir", str(tmp_path)])
        out = capsys.readouterr().out
        assert out.strip() == "comic-1: Fantastic Four #52 — 1 images — current bid $5.00 (1 bids)"

    def test_fetch_failed_line_format(self, tmp_path, capsys):
        """BUI-147: a fetch failure must print FETCH FAILED, never a
        0-images success line."""
        with patch("grade_photos.load_config", return_value=("id", "secret", ebay_fetch.PRODUCTION_BASE)):
            with patch("grade_photos.get_token", return_value="fake-token"):
                with patch("grade_photos.fetch_item_with_status", return_value=(None, 500)):
                    grade_photos.main(["999", "--workdir", str(tmp_path)])
        out = capsys.readouterr().out.strip()
        assert out.startswith("comic-1: FETCH FAILED — ")
        assert "images" not in out

    def test_comic_n_label_numbering_multiple_items(self, tmp_path, capsys):
        with patch("grade_photos.load_config", return_value=("id", "secret", ebay_fetch.PRODUCTION_BASE)):
            with patch("grade_photos.get_token", return_value="fake-token"):
                with patch(
                    "grade_photos.fetch_item_with_status",
                    side_effect=[(self._fake_item(), 200), (None, 500), (self._fake_item(), 200)],
                ):
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
                with patch("grade_photos.fetch_item_with_status", return_value=(self._fake_item(), 200)):
                    with patch("grade_photos._download_image"):
                        grade_photos.main(["111", "222", "--workdir", str(tmp_path)])
        assert mock_get_token.call_count == 1


# ============================================================
# BUI-310: refresh-on-401 mid-batch (closes the BUI-300 residual)
# ============================================================


class TestRefreshOn401:
    def _fake_item(self, title="Fantastic Four #52"):
        return {
            "title": title,
            "image": {"imageUrl": "https://example.com/main.jpg"},
            "additionalImages": [],
            "currentBidPrice": {"value": "5.00"},
            "bidCount": 1,
        }

    def test_401_mid_batch_refreshes_token_and_retries_once_then_succeeds(self, tmp_path, capsys):
        """A 401 on one item must trigger exactly one get_token() refresh and
        one retry of that same item — self-healing rather than FETCH FAILED —
        and the refreshed token must be reused for the rest of the batch."""
        with patch("grade_photos.load_config", return_value=("id", "secret", ebay_fetch.PRODUCTION_BASE)):
            with patch("grade_photos.get_token", side_effect=["stale-token", "fresh-token"]) as mock_get_token:
                with patch(
                    "grade_photos.fetch_item_with_status",
                    side_effect=[
                        (self._fake_item("First"), 200),
                        (None, 401),  # comic-2 with the stale token
                        (self._fake_item("Second-retried"), 200),  # comic-2 retried with fresh token
                        (self._fake_item("Third"), 200),  # comic-3 reuses the fresh token, no more 401s
                    ],
                ):
                    with patch("grade_photos._download_image"):
                        grade_photos.main(["111", "222", "333", "--workdir", str(tmp_path)])
        assert mock_get_token.call_count == 2
        # BUI-310: the refresh call must force a genuinely new token rather
        # than trust the cache, which could still hand back the just-rejected
        # one (see TestFetchItemWithStatus/get_token's force_refresh test).
        assert mock_get_token.call_args_list[1].kwargs.get("force_refresh") is True
        lines = capsys.readouterr().out.strip().splitlines()
        assert lines[0].startswith("comic-1: First")
        assert lines[1].startswith("comic-2: Second-retried")
        assert lines[2].startswith("comic-3: Third")
        assert "FETCH FAILED" not in "\n".join(lines)

    def test_refreshed_token_is_actually_forwarded_to_later_items(self, tmp_path, capsys):
        """Directly pins token propagation: the stale token is passed to
        comic-1 and comic-2's first (401) attempt, then the fresh token to
        comic-2's retry AND comic-3 — asserting on the actual token argument
        forwarded to fetch_item_with_status, not just inferring from the mock's
        positional return order."""
        with patch("grade_photos.load_config", return_value=("id", "secret", ebay_fetch.PRODUCTION_BASE)):
            with patch("grade_photos.get_token", side_effect=["stale-token", "fresh-token"]):
                with patch(
                    "grade_photos.fetch_item_with_status",
                    side_effect=[
                        (self._fake_item("First"), 200),  # comic-1, stale token
                        (None, 401),                       # comic-2, stale token → 401
                        (self._fake_item("Second"), 200),  # comic-2 retry, fresh token
                        (self._fake_item("Third"), 200),   # comic-3, fresh token
                    ],
                ) as mock_fetch:
                    with patch("grade_photos._download_image"):
                        grade_photos.main(["111", "222", "333", "--workdir", str(tmp_path)])
        # token is the second positional arg to fetch_item_with_status(item_id, token, base_url)
        tokens_used = [c.args[1] for c in mock_fetch.call_args_list]
        assert tokens_used == ["stale-token", "stale-token", "fresh-token", "fresh-token"]

    def test_get_token_systemexit_during_refresh_degrades_to_fetch_failed(self, tmp_path, capsys):
        """BUI-310 hardening: get_token() calls sys.exit(1) on a hard auth
        failure. That SystemExit (a BaseException) must NOT escape main()'s
        loop and abort the whole batch — it must degrade to this item's FETCH
        FAILED and let the remaining items run (BUI-147/BUI-300 invariant)."""
        with patch("grade_photos.load_config", return_value=("id", "secret", ebay_fetch.PRODUCTION_BASE)):
            # Initial token OK; the mid-batch force-refresh sys.exit(1)s.
            with patch("grade_photos.get_token", side_effect=["good-token", SystemExit(1)]) as mock_get_token:
                with patch(
                    "grade_photos.fetch_item_with_status",
                    side_effect=[(None, 401), (self._fake_item("Recovered"), 200)],
                ):
                    with patch("grade_photos._download_image"):
                        rc = grade_photos.main(["111", "222", "--workdir", str(tmp_path)])
        assert rc == 0  # batch completed, did not crash out via SystemExit
        assert mock_get_token.call_count == 2
        lines = capsys.readouterr().out.strip().splitlines()
        assert lines[0].startswith("comic-1: FETCH FAILED — ")
        # comic-2 still ran despite comic-1's refresh blowing up.
        assert lines[1].startswith("comic-2: Recovered")

    def test_401_persists_after_refresh_still_fetch_failed(self, tmp_path, capsys):
        """If the retry with a freshly fetched token ALSO 401s, that's a
        genuine failure — the FETCH FAILED contract must still hold, and the
        batch must not retry a second time."""
        with patch("grade_photos.load_config", return_value=("id", "secret", ebay_fetch.PRODUCTION_BASE)):
            with patch("grade_photos.get_token", side_effect=["stale-token", "still-bad-token"]) as mock_get_token:
                with patch(
                    "grade_photos.fetch_item_with_status",
                    side_effect=[(None, 401), (None, 401)],
                ):
                    grade_photos.main(["111", "--workdir", str(tmp_path)])
        assert mock_get_token.call_count == 2
        out = capsys.readouterr().out.strip()
        assert out.startswith("comic-1: FETCH FAILED — ")

    def test_non_401_failure_is_fetch_failed_without_refresh(self, tmp_path, capsys):
        """A non-401 failure (e.g. 500, 404) must NOT trigger a token refresh
        — only a 401 does. The FETCH FAILED contract stays intact unchanged."""
        with patch("grade_photos.load_config", return_value=("id", "secret", ebay_fetch.PRODUCTION_BASE)):
            with patch("grade_photos.get_token", return_value="fake-token") as mock_get_token:
                with patch("grade_photos.fetch_item_with_status", return_value=(None, 500)):
                    grade_photos.main(["111", "--workdir", str(tmp_path)])
        # Only the initial get_token() call before the loop — no refresh.
        assert mock_get_token.call_count == 1
        out = capsys.readouterr().out.strip()
        assert out.startswith("comic-1: FETCH FAILED — ")


# ============================================================
# BUI-322: cross-item fail-fast on a systemic 401 (revoked creds).
# BUI-310 gave each item its own refresh-and-retry with no memory across
# items, so a permanent 401 burned one force-refresh OAuth POST per
# remaining item. These tests pin the give-up threshold (two consecutive
# post-refresh 401s) and the reset condition (any success, including a
# self-heal, clears the streak).
# ============================================================


class TestSystemicFailFast:
    def _fake_item(self, title="Fantastic Four #52"):
        return {
            "title": title,
            "image": {"imageUrl": "https://example.com/main.jpg"},
            "additionalImages": [],
            "currentBidPrice": {"value": "5.00"},
            "bidCount": 1,
        }

    def test_systemic_401_gives_up_after_two_consecutive_post_refresh_401s(self, tmp_path, capsys):
        """Revoked creds: every item's retry-after-refresh still 401s. After
        the SECOND such item, the batch must stop POSTing a refresh for each
        remaining item — comic-3 and comic-4 get exactly one fetch attempt
        (no retry, no refresh call) instead of burning their own OAuth POST."""
        with patch("grade_photos.load_config", return_value=("id", "secret", ebay_fetch.PRODUCTION_BASE)):
            with patch(
                "grade_photos.get_token",
                side_effect=["t0", "t1", "t2"],
            ) as mock_get_token:
                with patch(
                    "grade_photos.fetch_item_with_status",
                    side_effect=[
                        (None, 401),  # comic-1 attempt 0 (stale token)
                        (None, 401),  # comic-1 attempt 1 (post-refresh #1 — still bad)
                        (None, 401),  # comic-2 attempt 0
                        (None, 401),  # comic-2 attempt 1 (post-refresh #2 — gives up)
                        (None, 401),  # comic-3 attempt 0 only — no refresh, no retry
                        (None, 401),  # comic-4 attempt 0 only — no refresh, no retry
                    ],
                ) as mock_fetch:
                    rc = grade_photos.main(["111", "222", "333", "444", "--workdir", str(tmp_path)])
        assert rc == 0
        # Initial token + exactly 2 refreshes (comic-1, comic-2) — comic-3
        # and comic-4 must NOT trigger a 3rd/4th force-refresh OAuth POST.
        assert mock_get_token.call_count == 3
        # 2 fetch attempts each for comic-1/comic-2 (attempt + retry), but
        # only 1 each for comic-3/comic-4 (no retry once given up).
        assert mock_fetch.call_count == 6
        lines = capsys.readouterr().out.strip().splitlines()
        assert len(lines) == 4
        assert all("FETCH FAILED" in line for line in lines)
        # comic-3/comic-4 skip the refresh path entirely — surfaced via the
        # give-up message rather than the plain post-refresh-401 message.
        assert "already failed earlier this batch" in lines[2]
        assert "already failed earlier this batch" in lines[3]

    def test_single_get_token_systemexit_does_not_give_up_alone(self, tmp_path, capsys):
        """BUI-322 (post-review correction): a SystemExit from
        get_token(force_refresh=True) is NOT treated as one-shot proof of a
        permanent failure — get_token() sys.exit(1)s on a network error/429/5xx
        that merely outlasted its own retry budget too, not just on revoked
        credentials. So ONE SystemExit must still let the NEXT item attempt
        its own refresh (counts toward the same two-in-a-row threshold as a
        post-refresh 401, not an immediate latch)."""
        with patch("grade_photos.load_config", return_value=("id", "secret", ebay_fetch.PRODUCTION_BASE)):
            with patch(
                "grade_photos.get_token",
                side_effect=["t0", SystemExit(1), "t1"],
            ) as mock_get_token:
                with patch(
                    "grade_photos.fetch_item_with_status",
                    side_effect=[
                        (None, 401),                     # comic-1 attempt 0 — refresh SystemExits
                        (None, 401),                      # comic-2 attempt 0
                        (self._fake_item("Two"), 200),     # comic-2 retry — refresh succeeded, self-heals
                    ],
                ):
                    with patch("grade_photos._download_image"):
                        rc = grade_photos.main(["111", "222", "--workdir", str(tmp_path)])
        assert rc == 0
        # Initial token + comic-1's SystemExit'ing refresh attempt + comic-2's
        # (successful) refresh attempt — comic-2 must NOT have been blocked
        # by comic-1's lone SystemExit.
        assert mock_get_token.call_count == 3
        lines = capsys.readouterr().out.strip().splitlines()
        assert lines[0].startswith("comic-1: FETCH FAILED — token refresh failed after 401")
        assert "already failed earlier this batch" not in lines[0]
        assert lines[1].startswith("comic-2: Two")

    def test_two_consecutive_get_token_systemexits_give_up(self, tmp_path, capsys):
        """Two consecutive SystemExits from get_token(force_refresh=True) —
        the same threshold as two consecutive post-refresh 401s — DO trigger
        give-up: the third item must not attempt its own refresh."""
        with patch("grade_photos.load_config", return_value=("id", "secret", ebay_fetch.PRODUCTION_BASE)):
            with patch(
                "grade_photos.get_token",
                side_effect=["t0", SystemExit(1), SystemExit(1)],
            ) as mock_get_token:
                with patch(
                    "grade_photos.fetch_item_with_status",
                    side_effect=[
                        (None, 401),  # comic-1 attempt 0 — refresh SystemExits (streak=1)
                        (None, 401),  # comic-2 attempt 0 — refresh SystemExits again (streak=2, gives up)
                        (None, 401),  # comic-3 attempt 0 — no refresh attempted (gave up)
                    ],
                ) as mock_fetch:
                    rc = grade_photos.main(["111", "222", "333", "--workdir", str(tmp_path)])
        assert rc == 0
        # Initial token + exactly 2 refresh attempts (both SystemExit) — no
        # 3rd refresh attempt for comic-3.
        assert mock_get_token.call_count == 3
        assert mock_fetch.call_count == 3
        lines = capsys.readouterr().out.strip().splitlines()
        assert lines[0].startswith("comic-1: FETCH FAILED — token refresh failed after 401")
        assert lines[1].startswith("comic-2: FETCH FAILED — token refresh failed after 401")
        assert "already failed earlier this batch" in lines[2]

    def test_systemexit_and_post_refresh_401_share_the_same_counter(self, tmp_path, capsys):
        """The two failure signatures — a SystemExit during refresh and a
        post-refresh 401 — must corroborate each other through ONE shared
        counter, not two independent ones: one of each in a row is still
        'two consecutive' and gives up."""
        with patch("grade_photos.load_config", return_value=("id", "secret", ebay_fetch.PRODUCTION_BASE)):
            with patch(
                "grade_photos.get_token",
                side_effect=["t0", SystemExit(1), "t1"],
            ) as mock_get_token:
                with patch(
                    "grade_photos.fetch_item_with_status",
                    side_effect=[
                        (None, 401),  # comic-1 attempt 0 — refresh SystemExits (streak=1)
                        (None, 401),  # comic-2 attempt 0
                        (None, 401),  # comic-2 attempt 1 — post-refresh 401 (streak=2, gives up)
                        (None, 401),  # comic-3 attempt 0 — no refresh attempted (gave up)
                    ],
                ) as mock_fetch:
                    rc = grade_photos.main(["111", "222", "333", "--workdir", str(tmp_path)])
        assert rc == 0
        assert mock_get_token.call_count == 3
        assert mock_fetch.call_count == 4
        lines = capsys.readouterr().out.strip().splitlines()
        assert "already failed earlier this batch" in lines[2]

    def test_transient_single_401_still_self_heals_counter_resets(self, tmp_path, capsys):
        """A single, isolated 401 must keep self-healing exactly like BUI-310
        — and a later, unrelated 401 later in the batch must ALSO get its own
        refresh-and-retry, proving a prior self-heal reset the streak rather
        than letting it silently accumulate toward the give-up threshold."""
        with patch("grade_photos.load_config", return_value=("id", "secret", ebay_fetch.PRODUCTION_BASE)):
            with patch(
                "grade_photos.get_token",
                side_effect=["t0", "t1", "t2"],
            ) as mock_get_token:
                with patch(
                    "grade_photos.fetch_item_with_status",
                    side_effect=[
                        (None, 401),                         # comic-1 attempt 0
                        (self._fake_item("One"), 200),        # comic-1 retry — self-heals
                        (self._fake_item("Two"), 200),        # comic-2 — plain success
                        (None, 401),                          # comic-3 attempt 0
                        (self._fake_item("Three"), 200),      # comic-3 retry — self-heals again
                    ],
                ):
                    with patch("grade_photos._download_image"):
                        rc = grade_photos.main(["111", "222", "333", "--workdir", str(tmp_path)])
        assert rc == 0
        # Initial token + refresh for comic-1 + refresh for comic-3: proves
        # comic-2's clean success reset the streak rather than the batch
        # giving up after comic-1's isolated 401.
        assert mock_get_token.call_count == 3
        out = capsys.readouterr().out.strip()
        assert "FETCH FAILED" not in out

    def test_intermittent_401s_interleaved_with_successes_do_not_trip_fail_fast(self, tmp_path, capsys):
        """Repeated post-refresh 401s that are never CONSECUTIVE (a clean
        success always lands in between) must never latch give_up_refreshing
        — each one keeps getting its own refresh attempt, per BUI-322's
        'two-in-a-row' semantics rather than a raw total count."""
        with patch("grade_photos.load_config", return_value=("id", "secret", ebay_fetch.PRODUCTION_BASE)):
            with patch(
                "grade_photos.get_token",
                side_effect=["t0", "t1", "t2", "t3"],
            ) as mock_get_token:
                with patch(
                    "grade_photos.fetch_item_with_status",
                    side_effect=[
                        (None, 401), (None, 401),             # comic-1: post-refresh 401 (counter=1)
                        (self._fake_item("Two"), 200),        # comic-2: success (resets counter)
                        (None, 401), (None, 401),             # comic-3: post-refresh 401 (counter=1)
                        (self._fake_item("Four"), 200),       # comic-4: success (resets counter)
                        (None, 401), (None, 401),             # comic-5: post-refresh 401 (counter=1)
                    ],
                ) as mock_fetch:
                    with patch("grade_photos._download_image"):
                        rc = grade_photos.main(["111", "222", "333", "444", "555", "--workdir", str(tmp_path)])
        assert rc == 0
        # Initial token + a refresh for comic-1, comic-3, and comic-5 — none
        # of the three post-refresh 401s ever triggered give-up.
        assert mock_get_token.call_count == 4
        assert mock_fetch.call_count == 8
        lines = capsys.readouterr().out.strip().splitlines()
        assert "already failed earlier this batch" not in "\n".join(lines)
        # comic-1/3/5 still fail (refresh didn't help THAT item), but via the
        # plain post-refresh-401 message, not the give-up message.
        assert lines[0].startswith("comic-1: FETCH FAILED — ")
        assert lines[2].startswith("comic-3: FETCH FAILED — ")
        assert lines[4].startswith("comic-5: FETCH FAILED — ")

    def test_proof_of_200_failure_between_two_post_refresh_401s_resets_counter(self, tmp_path, capsys):
        """BUI-331: the exact scenario the ticket names — a post-refresh 401,
        then a non-auth failure that occurred AFTER a proven HTTP-200 (a
        malformed item body), then more post-refresh 401s. The middle item got
        a 200, so the token demonstrably worked — that resets the streak,
        making the surrounding 401s NOT 'consecutive'. The batch must therefore
        NOT give up.

        FOUR items are required to observe this (BUI-331 code review): the
        give-up latch only changes a *subsequent* item's behavior, so a 3-item
        version (401 / proof-200 / 401) passes even against pre-BUI-331 code —
        the second 401 pair is the last item and its refresh happens either
        way. comic-4 is the observation point: with the reset, comic-1's and
        comic-3's 401s are NOT consecutive, give-up never latches, and comic-4
        still earns its own force-refresh. Without the reset (the old bug),
        comic-3 would be the 2nd consecutive post-refresh 401, give-up would
        latch, and comic-4 would be denied a refresh and print the give-up
        message. The get_token==4 / no-give-up-message assertions below fail
        against pre-BUI-331 code."""
        malformed = {"title": "Broken", "image": {}}  # 200 body, missing imageUrl → ListingContentError
        with patch("grade_photos.load_config", return_value=("id", "secret", ebay_fetch.PRODUCTION_BASE)):
            with patch(
                "grade_photos.get_token",
                side_effect=["t0", "t1", "t2", "t3"],
            ) as mock_get_token:
                with patch(
                    "grade_photos.fetch_item_with_status",
                    side_effect=[
                        (None, 401),        # comic-1 attempt 0
                        (None, 401),        # comic-1 attempt 1 — post-refresh 401 (counter=1)
                        (malformed, 200),   # comic-2 — proof-of-200 then malformed → resets counter to 0
                        (None, 401),        # comic-3 attempt 0
                        (None, 401),        # comic-3 attempt 1 — post-refresh 401 (counter=1, NOT 2)
                        (None, 401),        # comic-4 attempt 0
                        (None, 401),        # comic-4 attempt 1 — post-refresh 401 (counter=2; latches only now)
                    ],
                ) as mock_fetch:
                    with patch("grade_photos._download_image"):
                        rc = grade_photos.main(["111", "222", "333", "444", "--workdir", str(tmp_path)])
        assert rc == 0
        # Initial token + a refresh for comic-1, comic-3 AND comic-4 — comic-4
        # earning its own refresh proves comic-2's proven-200 reset the streak
        # (without the reset comic-3 would be the 2nd consecutive 401, give-up
        # would latch, and comic-4 would be refused a refresh: get_token==3).
        assert mock_get_token.call_count == 4
        assert mock_fetch.call_count == 7
        lines = capsys.readouterr().out.strip().splitlines()
        assert "already failed earlier this batch" not in "\n".join(lines)
        assert all("FETCH FAILED" in line for line in lines)

    def test_unparseable_200_body_is_proof_of_200_and_resets_counter(self, tmp_path, capsys):
        """BUI-331 (code-review follow-up): a 200 whose body does not parse —
        fetch_item_with_status returns (None, 200), the WAF-interstitial /
        truncated-proxy case — is ALSO proof the token worked (eBay returned
        200), so it must reset the give-up counter just like a malformed parsed
        body. Guards against the reset keying on 'non-None body' rather than on
        'observed 200': same 4-item shape as the test above but comic-2 is a
        (None, 200) instead of a parsed-malformed dict. comic-4 still earns its
        refresh and no give-up message prints."""
        with patch("grade_photos.load_config", return_value=("id", "secret", ebay_fetch.PRODUCTION_BASE)):
            with patch(
                "grade_photos.get_token",
                side_effect=["t0", "t1", "t2", "t3"],
            ) as mock_get_token:
                with patch(
                    "grade_photos.fetch_item_with_status",
                    side_effect=[
                        (None, 401),   # comic-1 attempt 0
                        (None, 401),   # comic-1 attempt 1 — post-refresh 401 (counter=1)
                        (None, 200),   # comic-2 — unparseable 200 body → ListingContentError → resets counter
                        (None, 401),   # comic-3 attempt 0
                        (None, 401),   # comic-3 attempt 1 — post-refresh 401 (counter=1, NOT 2)
                        (None, 401),   # comic-4 attempt 0
                        (None, 401),   # comic-4 attempt 1 — post-refresh 401 (counter=2; latches only now)
                    ],
                ) as mock_get_fetch:
                    rc = grade_photos.main(["111", "222", "333", "444", "--workdir", str(tmp_path)])
        assert rc == 0
        assert mock_get_token.call_count == 4
        assert mock_get_fetch.call_count == 7
        lines = capsys.readouterr().out.strip().splitlines()
        assert "already failed earlier this batch" not in "\n".join(lines)
        assert all("FETCH FAILED" in line for line in lines)

    def test_non_proof_failure_between_two_post_refresh_401s_still_gives_up(self, tmp_path, capsys):
        """BUI-331 (the load-bearing distinction / safety guard): a `data is
        None` non-401 fetch failure (a plain 500 — NO proven 200) landing
        between two post-refresh 401s must NOT reset the counter. The two 401s
        stay 'consecutive' and the batch gives up — proving the reset is gated
        on proof-of-200 specifically, not on any RuntimeError. A naive 'reset
        on any non-auth failure' regression would defeat the circuit breaker
        and keep hammering OAuth on a genuinely revoked-creds run."""
        with patch("grade_photos.load_config", return_value=("id", "secret", ebay_fetch.PRODUCTION_BASE)):
            with patch(
                "grade_photos.get_token",
                side_effect=["t0", "t1", "t2"],
            ) as mock_get_token:
                with patch(
                    "grade_photos.fetch_item_with_status",
                    side_effect=[
                        (None, 401),   # comic-1 attempt 0
                        (None, 401),   # comic-1 attempt 1 — post-refresh 401 (counter=1)
                        (None, 500),   # comic-2 — plain fetch failure, no proof of 200 → NO reset
                        (None, 401),   # comic-3 attempt 0
                        (None, 401),   # comic-3 attempt 1 — post-refresh 401 (counter=2 → give up)
                        (None, 401),   # comic-4 attempt 0 only — no refresh (gave up)
                    ],
                ) as mock_fetch:
                    rc = grade_photos.main(["111", "222", "333", "444", "--workdir", str(tmp_path)])
        assert rc == 0
        # Initial token + refresh for comic-1 + refresh for comic-3 only —
        # comic-4 must NOT trigger a 4th refresh (give-up latched after
        # comic-3's post-refresh 401, since comic-2's 500 did not reset).
        assert mock_get_token.call_count == 3
        assert mock_fetch.call_count == 6
        lines = capsys.readouterr().out.strip().splitlines()
        assert len(lines) == 4
        assert all("FETCH FAILED" in line for line in lines)
        assert "already failed earlier this batch" in lines[3]


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
