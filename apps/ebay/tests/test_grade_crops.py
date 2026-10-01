"""Tests for grade-crops (BUI-1083): overview + two labelled region sheets per photo."""

import hashlib
import time

import pytest
from PIL import Image

import grade_crops


def _make(folder, name, size):
    w, h = size
    im = Image.new("RGB", size)
    px = im.load()
    step_x, step_y = max(1, w // 64), max(1, h // 64)
    for x in range(0, w, step_x):
        for y in range(0, h, step_y):
            px[x, y] = (x * 255 // w, y * 255 // h, 128)
    im.save(folder / name, "JPEG")


def _run(capsys, src, dst):
    rc = grade_crops.main([str(src), str(dst)])
    out, err = capsys.readouterr()
    return rc, out.splitlines(), err


def test_two_photos_write_overview_and_two_sheets(tmp_path, capsys):
    src, dst = tmp_path / "comic-1", tmp_path / "comic-1" / "crops-grader-a"
    src.mkdir()
    _make(src, "img-01.jpg", (1250, 1600))
    _make(src, "img-02.jpg", (1250, 1600))
    (src / "notes.txt").write_text("not an image")

    rc, lines, err = _run(capsys, src, dst)

    assert rc == 0 and err == ""
    names = [p.rsplit("/", 1)[1] for p in lines]
    assert names == [
        "img-01-overview.jpg", "img-01-sheet-1.jpg", "img-01-sheet-2.jpg",
        "img-02-overview.jpg", "img-02-sheet-1.jpg", "img-02-sheet-2.jpg",
    ]
    # stdout is only absolute paths that exist; nothing else is written
    assert all(p.startswith("/") for p in lines)
    assert sorted(p.name for p in dst.iterdir()) == sorted(names)
    with Image.open(dst / "img-01-overview.jpg") as ov:
        assert max(ov.size) == 768 and ov.size == (600, 768)
    with Image.open(dst / "img-01-sheet-1.jpg") as sh:
        assert sh.size == (1024, 1024)  # four corners: 2 x 2
    with Image.open(dst / "img-01-sheet-2.jpg") as sh:
        assert sh.size == (1024, 2048)


def test_landscape_and_tiny_images(tmp_path, capsys):
    src, dst = tmp_path / "in", tmp_path / "out"
    src.mkdir()
    _make(src, "img-01.jpg", (1600, 900))
    _make(src, "img-02.jpg", (40, 30))  # smaller than the overview: never upscaled

    rc, lines, _ = _run(capsys, src, dst)

    assert rc == 0 and len(lines) == 6
    with Image.open(dst / "img-01-overview.jpg") as ov:
        assert ov.size == (768, 432)
    with Image.open(dst / "img-02-overview.jpg") as ov:
        assert ov.size == (40, 30)
    with Image.open(dst / "img-02-sheet-2.jpg") as sh:
        assert sh.size == (1024, 2048)  # eight tiles: 2 x 4


def test_numeric_order_and_deterministic(tmp_path, capsys):
    src = tmp_path / "in"
    src.mkdir()
    for n in (10, 2, 1):
        _make(src, f"img-{n:02d}.jpg", (300, 400))

    _, lines_a, _ = _run(capsys, src, tmp_path / "a")
    _, lines_b, _ = _run(capsys, src, tmp_path / "b")

    assert [p.rsplit("/", 1)[1] for p in lines_a][::3] == [
        "img-01-overview.jpg", "img-02-overview.jpg", "img-10-overview.jpg"]
    digest = lambda d: {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in d.iterdir()}  # noqa: E731
    assert digest(tmp_path / "a") == digest(tmp_path / "b")


def test_twenty_four_photos_is_fast(tmp_path, capsys):
    src = tmp_path / "in"
    src.mkdir()
    _make(src, "img-01.jpg", (1250, 1600))
    data = (src / "img-01.jpg").read_bytes()
    for n in range(2, 25):
        (src / f"img-{n:02d}.jpg").write_bytes(data)

    t0 = time.monotonic()
    rc, lines, _ = _run(capsys, src, tmp_path / "out")

    assert rc == 0 and len(lines) == 72
    assert time.monotonic() - t0 < 30


def test_empty_folder_and_missing_folder_fail_on_stderr(tmp_path, capsys):
    (tmp_path / "empty").mkdir()
    rc, lines, err = _run(capsys, tmp_path / "empty", tmp_path / "out")
    assert rc == 1 and lines == [] and "no img-NN.jpg" in err

    rc, lines, err = _run(capsys, tmp_path / "nope", tmp_path / "out")
    assert rc == 2 and lines == [] and "not a directory" in err


def test_unreadable_photo_reports_and_continues(tmp_path, capsys):
    src = tmp_path / "in"
    src.mkdir()
    (src / "img-01.jpg").write_bytes(b"not a jpeg")
    _make(src, "img-02.jpg", (300, 400))

    rc, lines, err = _run(capsys, src, tmp_path / "out")

    assert rc == 1 and len(lines) == 3 and "img-01.jpg" in err


def test_version_flag():
    with pytest.raises(SystemExit) as e:
        grade_crops.main(["--version"])
    assert e.value.code == 0


def _book_on_backdrop(size, box, backdrop=(10, 10, 10)):
    im = Image.new("RGB", size, backdrop)
    book = Image.new("RGB", (box[2] - box[0], box[3] - box[1]))
    _px = book.load()
    for x in range(book.width):
        for y in range(book.height):
            _px[x, y] = (200, 60 + (x * 150 // book.width), 40 + (y * 150 // book.height))
    im.paste(book, box[:2])
    return im


def test_book_corners_follow_the_book_on_a_backdrop():
    im = _book_on_backdrop((1250, 1600), (160, 60, 1090, 1490))
    tl, tr, bl, br = grade_crops.book_corners(im)
    for got, want in ((tl, (160, 60)), (tr, (1090, 60)), (bl, (160, 1490)), (br, (1090, 1490))):
        assert abs(got[0] - want[0]) <= 15 and abs(got[1] - want[1]) <= 15


def test_book_corners_fall_back_to_frame_on_full_bleed_or_tiny():
    full = _book_on_backdrop((1250, 1600), (0, 0, 1250, 1600))
    assert grade_crops.book_corners(full) == ((0, 0), (1250, 0), (0, 1600), (1250, 1600))
    tiny = Image.new("RGB", (40, 30))
    assert grade_crops.book_corners(tiny) == ((0, 0), (40, 0), (0, 30), (40, 30))


def test_sheet_2_covers_both_spine_edges_top_and_bottom():
    """BUI-1083: a back-cover photo's spine is the right edge, so the right
    edge needs the same top/bottom tiles as the left (front-cover spine)."""
    labels = [r[0] for r in grade_crops.SHEET_2]
    for edge in ("left", "right"):
        assert f"{edge} edge top" in labels and f"{edge} edge bottom" in labels
    assert len(labels) == 8

    corners = ((100, 50), (900, 50), (100, 1450), (900, 1450))
    side = 160
    centre = {r[0]: grade_crops._region_centre(corners, r[1], r[2], r[3], side)
              for r in grade_crops.SHEET_2}
    # right-edge top/bottom sit on the right edge, one region in from the corners
    assert centre["right edge top"] == (900, 50 + side)
    assert centre["right edge bottom"] == (900, 1450 - side)
    assert centre["left edge top"] == (100, 50 + side)
