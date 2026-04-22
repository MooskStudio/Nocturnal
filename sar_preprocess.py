#!/usr/bin/env python3
"""
sar_preprocess.py — NOCTURNAL Phase 3: Sentinel-1 preprocessing + label prep.

Two responsibilities, in one module:

  (A) grd_to_tiles():
        Turn a Sentinel-1 IW GRD .SAFE directory into a folder of 8-bit
        PNG tiles that YOLOv8 can ingest. Applies sigma-nought (σ⁰)
        calibration using the product's own calibration LUT, clips the
        dB range to a sensible ocean window, and writes tiles with a
        configurable overlap.

  (B) lsssdd_to_yolo():
        Convert LS-SSDD-v1.0 style Pascal-VOC XML annotations to YOLO
        .txt label files, one line per bounding box:
            class_id cx cy w h    (all normalised to [0, 1])

Cross-platform. Dependencies: numpy, rasterio, pillow, scipy.
    pip install numpy rasterio pillow scipy
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Optional, Tuple

try:
    import numpy as np
    import rasterio
    from rasterio.windows import Window
    from PIL import Image
    from scipy.interpolate import RegularGridInterpolator
except ImportError as e:
    raise SystemExit(
        "sar_preprocess.py needs: pip install numpy rasterio pillow scipy\n"
        f"Missing package: {getattr(e, 'name', e)}"
    )


# ────────────────────────────────────────────────────────────────────
# Part A. GRD → tiles
# ────────────────────────────────────────────────────────────────────

# Clip range for σ⁰ in dB. Ocean is typically -30 → -5 dB; land/ships
# saturate brighter. A 50 dB dynamic range maps cleanly to 8-bit.
DB_MIN, DB_MAX = -35.0, 5.0


def _locate(safe: Path, glob: str) -> Path:
    safe = Path(safe)
    if safe.suffix.lower() == ".zip":
        raise ValueError(f"Unzip the .SAFE archive first: {safe}")
    matches = list(safe.glob(glob))
    if not matches:
        raise FileNotFoundError(f"no match for {glob} under {safe}")
    return matches[0]


def locate_measurement(safe: Path, pol: str = "vv") -> Path:
    return _locate(safe, f"measurement/*-{pol.lower()}-*.tiff")


def locate_calibration(safe: Path, pol: str = "vv") -> Path:
    return _locate(safe, f"annotation/calibration/calibration-*-{pol.lower()}-*.xml")


def _parse_calibration_lut(xml_path: Path
                          ) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Return (lines, pixels, sigma0_lut) where sigma0_lut is 2D."""
    tree = ET.parse(xml_path)
    vectors = tree.getroot().findall(".//calibrationVector")
    lines:  List[int]          = []
    sigma:  List[List[float]]  = []
    pixels: Optional[np.ndarray] = None
    for v in vectors:
        lines.append(int(v.find("line").text))
        px = np.fromstring(v.find("pixel").text, sep=" ", dtype=np.int32)
        if pixels is None:
            pixels = px
        sigma.append([float(x) for x in v.find("sigmaNought").text.split()])
    return (np.asarray(lines, dtype=np.int32),
            pixels if pixels is not None else np.zeros(0, np.int32),
            np.asarray(sigma, dtype=np.float32))


def _interp_lut(shape_hw: Tuple[int, int],
                lines: np.ndarray, pixels: np.ndarray,
                sigma: np.ndarray,
                row_off: int = 0, col_off: int = 0) -> np.ndarray:
    """
    Upsample the coarse calibration LUT to the full window shape using
    bilinear interpolation on a regular grid.
    """
    H, W = shape_hw
    f = RegularGridInterpolator(
        (lines, pixels), sigma,
        bounds_error=False, fill_value=None, method="linear")
    rr = np.arange(H, dtype=np.float32) + row_off
    cc = np.arange(W, dtype=np.float32) + col_off
    R, C = np.meshgrid(rr, cc, indexing="ij")
    return f((R, C)).astype(np.float32)


def grd_sigma0_db(safe: Path, pol: str = "vv",
                  window: Optional[Window] = None) -> np.ndarray:
    """
    Read DN (optionally a window), apply the product's σ⁰ calibration LUT,
    and return sigma-nought in decibels, clipped to [DB_MIN, DB_MAX].
    """
    tif = locate_measurement(safe, pol)
    with rasterio.open(tif) as src:
        win = window or Window(0, 0, src.width, src.height)
        dn = src.read(1, window=win).astype(np.float32)
    lines, pixels, sigma = _parse_calibration_lut(
        locate_calibration(safe, pol))
    lut = _interp_lut(dn.shape, lines, pixels, sigma,
                      row_off=int(win.row_off), col_off=int(win.col_off))
    sigma0_lin = (dn ** 2) / np.maximum(lut ** 2, 1e-6)
    sigma0_db  = 10.0 * np.log10(np.maximum(sigma0_lin, 1e-10))
    return np.clip(sigma0_db, DB_MIN, DB_MAX)


def _db_to_u8(db: np.ndarray) -> np.ndarray:
    """Map dB in [DB_MIN, DB_MAX] to uint8 [0, 255] for PNG output."""
    norm = (db - DB_MIN) / (DB_MAX - DB_MIN)
    return (np.clip(norm, 0.0, 1.0) * 255.0).astype(np.uint8)


@dataclass
class TileSpec:
    row: int             # top row in full scene (px)
    col: int             # left col in full scene (px)
    height: int
    width: int


def _plan_tiles(H: int, W: int, tile: int, overlap: int) -> List[TileSpec]:
    step = tile - overlap
    rows = list(range(0, max(1, H - overlap), step))
    cols = list(range(0, max(1, W - overlap), step))
    # Ensure the right/bottom edge is covered by snapping the last tile
    if rows[-1] + tile > H:
        rows[-1] = max(0, H - tile)
    if cols[-1] + tile > W:
        cols[-1] = max(0, W - tile)
    return [TileSpec(r, c, min(tile, H - r), min(tile, W - c))
            for r in rows for c in cols]


def grd_to_tiles(safe: Path, out_dir: Path,
                 pol: str = "vv",
                 tile: int = 800, overlap: int = 80,
                 stem: Optional[str] = None,
                 skip_blank: bool = True) -> List[Path]:
    """
    Write calibrated σ⁰_dB tiles as 8-bit PNGs into out_dir.
    Returns the list of output paths. An index JSON is also written so
    Phase 4 can reconstruct tile → full-scene pixel coordinates.
    """
    safe    = Path(safe)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = stem or safe.name.replace(".SAFE", "")

    tif = locate_measurement(safe, pol)
    with rasterio.open(tif) as src:
        H, W = src.height, src.width

    plans = _plan_tiles(H, W, tile, overlap)
    out_paths: List[Path] = []
    manifest = {
        "scene": safe.name, "polarisation": pol,
        "full_shape": [H, W], "tile": tile, "overlap": overlap,
        "tiles": [],
    }

    for i, t in enumerate(plans):
        db = grd_sigma0_db(safe, pol,
                           window=Window(t.col, t.row, t.width, t.height))
        # Skip tiles that are essentially all-ocean with no bright targets;
        # this is a cheap optimisation for training set creation. In
        # inference mode (skip_blank=False) every tile is kept.
        if skip_blank and db.max() < -15.0:
            continue
        u8 = _db_to_u8(db)
        name = f"{stem}_r{t.row:06d}_c{t.col:06d}.png"
        path = out_dir / name
        Image.fromarray(u8, mode="L").save(path, format="PNG")
        out_paths.append(path)
        manifest["tiles"].append({
            "file": name, "row": t.row, "col": t.col,
            "h": t.height, "w": t.width,
        })

    with open(out_dir / f"{stem}_tiles.json", "w") as f:
        json.dump(manifest, f, indent=2)

    return out_paths


# ────────────────────────────────────────────────────────────────────
# Part B. LS-SSDD (Pascal-VOC XML) → YOLO .txt
# ────────────────────────────────────────────────────────────────────

def voc_xml_to_yolo(xml_path: Path, class_map: dict[str, int]) -> List[str]:
    """Parse a single Pascal-VOC XML and return YOLO-format label lines."""
    tree = ET.parse(xml_path)
    root = tree.getroot()
    sz = root.find("size")
    W = float(sz.find("width").text)
    H = float(sz.find("height").text)
    lines: List[str] = []
    for obj in root.findall("object"):
        name = (obj.find("name").text or "").strip().lower()
        cls  = class_map.get(name)
        if cls is None:
            continue
        b = obj.find("bndbox")
        x1 = float(b.find("xmin").text); y1 = float(b.find("ymin").text)
        x2 = float(b.find("xmax").text); y2 = float(b.find("ymax").text)
        cx = ((x1 + x2) / 2.0) / W
        cy = ((y1 + y2) / 2.0) / H
        bw = (x2 - x1) / W
        bh = (y2 - y1) / H
        # Drop degenerate boxes
        if bw <= 0 or bh <= 0:
            continue
        lines.append(f"{cls} {cx:.6f} {cy:.6f} {bw:.6f} {bh:.6f}")
    return lines


def lsssdd_to_yolo(xml_dir: Path, out_dir: Path,
                   class_map: Optional[dict[str, int]] = None) -> int:
    """
    Convert all Pascal-VOC XMLs in xml_dir to YOLO .txt files in out_dir.
    Default class map: {'ship': 0}.  Returns the number of files written.
    """
    xml_dir = Path(xml_dir)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    class_map = class_map or {"ship": 0}

    count = 0
    for xml in xml_dir.glob("*.xml"):
        lines = voc_xml_to_yolo(xml, class_map)
        (out_dir / f"{xml.stem}.txt").write_text("\n".join(lines) + "\n")
        count += 1
    return count


# ────────────────────────────────────────────────────────────────────
# CLI
# ────────────────────────────────────────────────────────────────────

def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    t = sub.add_parser("tile",
        help="tile a SAFE product into 8-bit PNG tiles")
    t.add_argument("safe", type=Path, help="path to an unzipped .SAFE directory")
    t.add_argument("--out", type=Path, required=True, help="output folder")
    t.add_argument("--pol", default="vv", choices=["vv", "vh"])
    t.add_argument("--tile", type=int, default=800)
    t.add_argument("--overlap", type=int, default=80)
    t.add_argument("--keep-blank", action="store_true",
                   help="keep ocean-only tiles (default: skip). Use for inference.")

    l = sub.add_parser("labels",
        help="convert LS-SSDD Pascal-VOC XML labels to YOLO .txt")
    l.add_argument("xml_dir", type=Path, help="folder of *.xml annotations")
    l.add_argument("--out", type=Path, required=True, help="folder to write .txt into")

    args = ap.parse_args(argv)

    if args.cmd == "tile":
        paths = grd_to_tiles(args.safe, args.out,
                             pol=args.pol, tile=args.tile, overlap=args.overlap,
                             skip_blank=not args.keep_blank)
        print(f"[ok] wrote {len(paths)} tiles to {args.out}")
    elif args.cmd == "labels":
        n = lsssdd_to_yolo(args.xml_dir, args.out)
        print(f"[ok] converted {n} xml → yolo .txt in {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
