#!/usr/bin/env python3
"""grade-crops: deterministic overview + region contact sheets for the grader.

BUI-1083: the comic-grader agent used to crop ambiguous details one at a
time (one Bash call plus one Read per crop, each a full turn that re-reads
the whole context). This script makes the standard crops in one call so the
grader can Read every view in a single turn.

    grade-crops <image folder> <crop dir>

For every ``img-NN.jpg`` in the image folder it writes into the crop dir:

- ``img-NN-overview.jpg`` -- the whole photo, downscaled to a 768 px long
  edge (never upscaled).
- ``img-NN-sheet-1.jpg`` -- the four corners (2x2 grid).
- ``img-NN-sheet-2.jpg`` -- left edge top and bottom (the spine on a front
  cover), the staple area (left edge at 30% and 70% of the height), right
  edge top, bottom and middle (the spine on a back cover), and the center
  (2x4 grid).

Tiles are 512 px. Regions are located on the comic itself, not the photo
frame: on a near-uniform backdrop the four book corners are detected (a
tilted or keystoned book is followed), and corners/edges are centred on the
book's outline so each tile shows the edge against the backdrop. A
full-bleed or busy photo falls back to the frame. Each region's side is 20%
of the book's short side, enlarged to the tile (about 2x on a typical
1250x1600 eBay photo), with its name and zoom drawn into the tile.

stdout carries only the written paths, one per line (the grader Reads them
as-is). Errors go to stderr with a non-zero exit. PIL only, no network,
deterministic for a given input.
"""
from __future__ import annotations

import argparse
import importlib.metadata
import re
import sys
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont, ImageOps

OVERVIEW_LONG_EDGE = 768
TILE = 512
REGION_FRACTION = 0.2  # of the book's short side: ~2x on a 1250x1600 photo
MIN_REGION = 8
JPEG_QUALITY = 85
_DETECT_EDGE = 256  # book detection runs on a copy this size (long edge)
_BG_THRESHOLD = 28  # per-pixel distance from the backdrop colour (0-255)
_BG_MAX_SPREAD = 30  # backdrop must be near-uniform to trust detection
_MIN_BOOK_SHARE = 0.3  # a book covering less of the frame is distrusted

_IMG_RE = re.compile(r"^img-(\d+)\.jpe?g$", re.IGNORECASE)

# Regions as (label, edge, t, dt): a point at fraction t along one edge of
# the detected book outline, shifted dt region-sides along that edge. Corners
# and edges are centred ON the book's outline so each tile shows the edge
# against the backdrop. Labels are positional, not "spine": on a front-cover
# photo the spine is the left edge, on a back-cover photo the right edge.
SHEET_1 = (
    ("top-left corner", "left", 0.0, 0.0),
    ("top-right corner", "right", 0.0, 0.0),
    ("bottom-left corner", "left", 1.0, 0.0),
    ("bottom-right corner", "right", 1.0, 0.0),
)
SHEET_2 = (
    ("left edge top", "left", 0.0, 1.0),
    ("left edge bottom", "left", 1.0, -1.0),
    ("left edge upper staple", "left", 0.3, 0.0),
    ("left edge lower staple", "left", 0.7, 0.0),
    ("right edge top", "right", 0.0, 1.0),
    ("right edge bottom", "right", 1.0, -1.0),
    ("right edge middle", "right", 0.5, 0.0),
    ("center", "center", 0.0, 0.0),
)


def _version_string() -> str:
    try:
        pkg_version = importlib.metadata.version("ebay-tools")
    except importlib.metadata.PackageNotFoundError:
        pkg_version = "unknown"
    try:
        from _ebay_build_stamp import GIT_DATE, GIT_SHA
    except ImportError:
        GIT_SHA, GIT_DATE = "unknown", "unknown"
    return f"grade-crops {pkg_version} (git {GIT_SHA}, {GIT_DATE})"


def find_images(folder: Path) -> list[Path]:
    """Return the folder's img-NN.jpg files in numeric order."""
    found = []
    for p in folder.iterdir():
        m = _IMG_RE.match(p.name)
        if m and p.is_file():
            found.append((int(m.group(1)), p.name, p))
    return [p for _, _, p in sorted(found)]


def _font():
    try:
        return ImageFont.load_default(size=26)
    except TypeError:  # Pillow < 10.1 has no sized default font
        return ImageFont.load_default()


def book_corners(im: Image.Image):
    """The comic's four corners (tl, tr, bl, br) on a uniform backdrop.

    Samples the photo's outer frame for the backdrop colour; when that frame
    is near-uniform, each corner is the book pixel nearest that corner of the
    frame, which follows a tilted or keystoned book. A full-bleed or busy
    photo (non-uniform frame) or an implausibly small book falls back to the
    frame's own corners.
    """
    w, h = im.size
    frame_corners = ((0, 0), (w, 0), (0, h), (w, h))
    if min(w, h) < 64:
        return frame_corners
    small = im.copy()
    small.thumbnail((_DETECT_EDGE, _DETECT_EDGE), Image.Resampling.BOX)
    sw, sh = small.size
    px = small.load()
    frame = [px[x, y] for x in range(sw) for y in (0, 1, sh - 2, sh - 1)]
    frame += [px[x, y] for y in range(sh) for x in (0, 1, sw - 2, sw - 1)]
    bg = tuple(sorted(c[i] for c in frame)[len(frame) // 2] for i in range(3))
    spread = sum(max(abs(c[i] - bg[i]) for i in range(3)) for c in frame) / len(frame)
    if spread > _BG_MAX_SPREAD:
        return frame_corners
    diff = ImageChops.difference(small, _backdrop_model(small))
    diff = diff.filter(ImageFilter.BoxBlur(1))  # damp backdrop texture/JPEG noise
    mask = ImageChops.lighter(
        ImageChops.lighter(*diff.split()[:2]), diff.split()[2]
    ).point(lambda v: 255 if v > _BG_THRESHOLD else 0)
    mask = mask.filter(ImageFilter.MinFilter(3)).filter(ImageFilter.MaxFilter(3))
    data = mask.tobytes()
    book = [(i % sw, i // sw) for i, v in enumerate(data) if v]
    if len(book) < _MIN_BOOK_SHARE * sw * sh:
        return frame_corners
    tl = min(book, key=lambda p: p[0] + p[1])
    tr = min(book, key=lambda p: (sw - p[0]) + p[1])
    bl = min(book, key=lambda p: p[0] + (sh - p[1]))
    br = min(book, key=lambda p: (sw - p[0]) + (sh - p[1]))
    sx, sy = w / sw, h / sh
    return (
        (tl[0] * sx, tl[1] * sy),
        ((tr[0] + 1) * sx, tr[1] * sy),
        (bl[0] * sx, (bl[1] + 1) * sy),
        ((br[0] + 1) * sx, (br[1] + 1) * sy),
    )


def _backdrop_model(small: Image.Image) -> Image.Image:
    """Smooth backdrop estimate: the photo's outer frame stretched inward.

    Blends the top/bottom frame rows (by height) with the left/right frame
    columns (by width), so a backdrop with a lighting gradient or vignette
    is not mistaken for the book.
    """
    sw, sh = small.size
    soft = small.filter(ImageFilter.BoxBlur(2))
    near = Image.Resampling.NEAREST

    def stretch(box):
        return soft.crop(box).resize((sw, sh), near)

    ramp = Image.linear_gradient("L")  # black top -> white bottom
    vgrad = ramp.resize((sw, sh))
    hgrad = ramp.transpose(Image.Transpose.ROTATE_90).resize((sw, sh))  # black left
    tb = Image.composite(stretch((0, sh - 2, sw, sh - 1)), stretch((0, 1, sw, 2)), vgrad)
    lr = Image.composite(stretch((sw - 2, 0, sw - 1, sh)), stretch((1, 0, 2, sh)), hgrad)
    return Image.blend(tb, lr, 0.5)


def _region_centre(corners, edge: str, t: float, dt: float, side: int):
    tl, tr, bl, br = corners
    if edge == "center":
        return (sum(p[0] for p in corners) / 4, sum(p[1] for p in corners) / 4)
    a, b = (tl, bl) if edge == "left" else (tr, br)
    length = max(1.0, ((b[0] - a[0]) ** 2 + (b[1] - a[1]) ** 2) ** 0.5)
    t = t + dt * side / length
    return (a[0] + t * (b[0] - a[0]), a[1] + t * (b[1] - a[1]))


def _box(size, centre, side: int):
    w, h = size
    side_w, side_h = min(side, w), min(side, h)
    left = max(0, min(round(centre[0] - side_w / 2), w - side_w))
    top = max(0, min(round(centre[1] - side_h / 2), h - side_h))
    return (left, top, left + side_w, top + side_h)


def _tile(im: Image.Image, box, label: str, font) -> Image.Image:
    region = im.crop(box)
    tile = region.resize((TILE, TILE), Image.Resampling.LANCZOS)
    draw = ImageDraw.Draw(tile)
    text = f"{label}  x{TILE / max(1, box[2] - box[0]):.1f}"
    l, t, r, b = draw.textbbox((0, 0), text, font=font)
    draw.rectangle((0, 0, r - l + 12, b - t + 12), fill=(0, 0, 0))
    draw.text((6 - l, 6 - t), text, fill=(255, 255, 0), font=font)
    return tile


def _sheet(im: Image.Image, corners, regions, side: int, font) -> Image.Image:
    rows = -(-len(regions) // 2)
    sheet = Image.new("RGB", (2 * TILE, rows * TILE), (255, 255, 255))
    for i, (label, edge, t, dt) in enumerate(regions):
        box = _box(im.size, _region_centre(corners, edge, t, dt, side), side)
        sheet.paste(_tile(im, box, label, font), ((i % 2) * TILE, (i // 2) * TILE))
    # white gutters so adjacent tiles never read as one continuous region
    draw = ImageDraw.Draw(sheet)
    draw.line((TILE, 0, TILE, rows * TILE), fill=(255, 255, 255), width=4)
    for r in range(1, rows):
        draw.line((0, r * TILE, 2 * TILE, r * TILE), fill=(255, 255, 255), width=4)
    return sheet


def _dist(a, b) -> float:
    return ((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2) ** 0.5


def crop_one(src: Path, out_dir: Path, font) -> list[Path]:
    with Image.open(src) as raw:
        im = ImageOps.exif_transpose(raw).convert("RGB")
    stem = src.stem.lower()
    written = []

    overview = im.copy()
    overview.thumbnail((OVERVIEW_LONG_EDGE, OVERVIEW_LONG_EDGE), Image.Resampling.LANCZOS)
    p = out_dir / f"{stem}-overview.jpg"
    overview.save(p, "JPEG", quality=JPEG_QUALITY)
    written.append(p)

    corners = book_corners(im)
    tl, tr, bl, br = corners
    short = min((_dist(tl, tr) + _dist(bl, br)) / 2, (_dist(tl, bl) + _dist(tr, br)) / 2)
    side = max(MIN_REGION, round(REGION_FRACTION * short))
    for n, regions in ((1, SHEET_1), (2, SHEET_2)):
        p = out_dir / f"{stem}-sheet-{n}.jpg"
        _sheet(im, corners, regions, side, font).save(p, "JPEG", quality=JPEG_QUALITY)
        written.append(p)
    return written


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="grade-crops",
        description="Write a 768 px overview and two labelled region contact "
        "sheets (corners; left edge top/bottom, staple area, right edge top/bottom/middle, center) "
        "for every "
        "img-NN.jpg in IMAGE_FOLDER. Prints only the written paths.",
    )
    parser.add_argument("image_folder", type=Path)
    parser.add_argument("crop_dir", type=Path)
    parser.add_argument("--version", action="version", version=_version_string())
    args = parser.parse_args(argv)

    if not args.image_folder.is_dir():
        print(f"grade-crops: not a directory: {args.image_folder}", file=sys.stderr)
        return 2
    images = find_images(args.image_folder)
    if not images:
        print(f"grade-crops: no img-NN.jpg in {args.image_folder}", file=sys.stderr)
        return 1
    crop_dir = args.crop_dir.resolve()
    crop_dir.mkdir(parents=True, exist_ok=True)

    font = _font()
    failed = 0
    for src in images:
        try:
            paths = crop_one(src, crop_dir, font)
        except (OSError, ValueError) as e:
            print(f"grade-crops: {src.name}: {e}", file=sys.stderr)
            failed += 1
            continue
        for p in paths:
            print(p)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
