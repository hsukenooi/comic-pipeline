"""Tests for grade-photos photo triage (BUI-1084), using synthetic PIL images."""

import random

from PIL import Image

import grade_photos


def _noise(seed, size=(400, 500)):
    """A deterministic textured image; different seeds hash far apart."""
    rng = random.Random(seed)
    img = Image.new("L", (8, 8))
    img.putdata([rng.randrange(256) for _ in range(64)])
    return img.resize(size, Image.NEAREST).convert("RGB")


def _write(folder, n, img):
    folder.mkdir(parents=True, exist_ok=True)
    img.save(folder / f"img-{n:02d}.jpg", quality=95)


def _names(folder):
    return sorted(p.name for p in folder.glob("img-*.jpg"))


def test_near_duplicate_dropped_and_survivors_renumbered(tmp_path):
    a, b = _noise(1), _noise(2)
    _write(tmp_path, 1, a)
    _write(tmp_path, 2, a.resize((420, 520)))  # same picture, different size
    _write(tmp_path, 3, b)
    kept, dropped = grade_photos.triage_images(tmp_path)
    assert kept == 2
    assert dropped == [(2, "near-duplicate of img-01")]
    assert _names(tmp_path) == ["img-01.jpg", "img-02.jpg"]
    # img-02 is now the old img-03 (the distinct picture).
    assert Image.open(tmp_path / "img-02.jpg").size == (400, 500)


def test_tiny_image_dropped(tmp_path):
    _write(tmp_path, 1, _noise(1))
    _write(tmp_path, 2, _noise(2, size=(120, 160)))
    kept, dropped = grade_photos.triage_images(tmp_path)
    assert kept == 1
    assert dropped[0][0] == 2 and dropped[0][1].startswith("too small")


def test_extreme_aspect_dropped(tmp_path):
    _write(tmp_path, 1, _noise(1))
    _write(tmp_path, 2, _noise(2, size=(1600, 300)))
    kept, dropped = grade_photos.triage_images(tmp_path)
    assert kept == 1
    assert dropped[0][1].startswith("extreme aspect")


def test_first_image_never_dropped(tmp_path):
    # A tiny, extreme-aspect first image still survives, and later images are judged on their own.
    _write(tmp_path, 1, _noise(1, size=(100, 900)))
    _write(tmp_path, 2, _noise(2))
    kept, dropped = grade_photos.triage_images(tmp_path)
    assert kept == 2 and dropped == []
    assert _names(tmp_path) == ["img-01.jpg", "img-02.jpg"]


def test_first_image_kept_even_over_cap(monkeypatch, tmp_path):
    monkeypatch.setattr(grade_photos, "MAX_PHOTOS", 0)
    _write(tmp_path, 1, _noise(1))
    _write(tmp_path, 2, _noise(2))
    kept, dropped = grade_photos.triage_images(tmp_path)
    assert kept == 1
    assert [n for n, _ in dropped] == [2]
    assert _names(tmp_path) == ["img-01.jpg"]


def test_cap_prefers_listing_order(monkeypatch, tmp_path):
    monkeypatch.setattr(grade_photos, "MAX_PHOTOS", 3)
    for n in range(1, 7):
        _write(tmp_path, n, _noise(n))
    kept, dropped = grade_photos.triage_images(tmp_path)
    assert kept == 3
    assert [n for n, _ in dropped] == [4, 5, 6]
    assert all("cap" in why for _, why in dropped)
    assert _names(tmp_path) == ["img-01.jpg", "img-02.jpg", "img-03.jpg"]


def test_unreadable_file_is_kept(tmp_path):
    _write(tmp_path, 1, _noise(1))
    (tmp_path / "img-02.jpg").write_bytes(b"fake-image-bytes")
    kept, dropped = grade_photos.triage_images(tmp_path)
    assert kept == 2 and dropped == []


def test_blank_pages_are_not_treated_as_duplicates(tmp_path):
    _write(tmp_path, 1, _noise(1))
    _write(tmp_path, 2, Image.new("RGB", (400, 500), "white"))
    _write(tmp_path, 3, Image.new("RGB", (400, 500), "white"))
    kept, dropped = grade_photos.triage_images(tmp_path)
    assert kept == 3 and dropped == []
