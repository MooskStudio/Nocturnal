#!/usr/bin/env python3
"""
yolo_infer_sar.py — NOCTURNAL Phase 3: sliding-window inference on a
full Sentinel-1 GRD scene.

Flow:
  1. Tile the SAFE with overlap (reuses sar_preprocess.grd_to_tiles, but
     with skip_blank=False so every tile is scored).
  2. Run the fine-tuned YOLOv8 model on each tile.
  3. Translate per-tile box coordinates back into full-scene pixel
     coordinates and apply global Non-Maximum Suppression to remove
     duplicates in the overlap strips.
  4. Upsert each surviving detection into a `detections` table inside
     ais_memory.db. Pixel → GPS conversion is deliberately deferred to
     Phase 4, where rasterio's geolocation model does it correctly.

A per-scene run looks like this:

    python yolo_infer_sar.py <PATH_TO.SAFE> \\
        --weights runs/nocturnal/yolov8n-sar-stage2-finetune/weights/best.pt \\
        --tile 800 --overlap 80 --conf 0.2

Install:
    pip install ultralytics torch rasterio pillow numpy
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, List, Optional, Tuple

try:
    import numpy as np
    from PIL import Image
except ImportError as e:
    raise SystemExit("yolo_infer_sar.py needs: pip install numpy pillow") from e

# Local imports (Phase 1 + Phase 3 preprocessing)
from sar_preprocess import grd_to_tiles   # noqa: E402


DEFAULT_DB_PATH = Path(__file__).resolve().parent / "ais_memory.db"


# ────────────────────── schema migration ──────────────────────

_SCHEMA_DETECTIONS = """
CREATE TABLE IF NOT EXISTS detections (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id   TEXT,                    -- CDSE UUID (may be NULL if run ad-hoc)
    scene_name   TEXT NOT NULL,           -- SAFE folder name
    polarisation TEXT NOT NULL,
    pixel_x      REAL NOT NULL,           -- column in full scene
    pixel_y      REAL NOT NULL,           -- row   in full scene
    width_px     REAL NOT NULL,
    height_px    REAL NOT NULL,
    confidence   REAL NOT NULL,
    class_id     INTEGER NOT NULL,
    model_name   TEXT,
    -- Phase-4 fields, filled later by the matcher:
    lat          REAL,
    lon          REAL,
    matched_mmsi INTEGER,
    match_dist_m REAL,
    dark         INTEGER,                  -- 0/1 flag after AIS cross-ref
    score        REAL,                     -- Phase-5 human-crossing score
    created_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_det_scene ON detections(scene_name);
CREATE INDEX IF NOT EXISTS idx_det_prod  ON detections(product_id);
"""


def ensure_detection_schema(db_path: os.PathLike) -> None:
    with sqlite3.connect(os.fspath(db_path), timeout=30) as c:
        c.executescript(
            "PRAGMA journal_mode=WAL;"
            "PRAGMA synchronous=NORMAL;"
        )
        c.executescript(_SCHEMA_DETECTIONS)
        c.commit()


# ────────────────────── helpers ──────────────────────

@dataclass
class Detection:
    pixel_x: float   # centre column in full scene
    pixel_y: float   # centre row    in full scene
    w: float
    h: float
    conf: float
    cls:  int


def _iou(a: Detection, b: Detection) -> float:
    ax1, ay1 = a.pixel_x - a.w / 2, a.pixel_y - a.h / 2
    ax2, ay2 = a.pixel_x + a.w / 2, a.pixel_y + a.h / 2
    bx1, by1 = b.pixel_x - b.w / 2, b.pixel_y - b.h / 2
    bx2, by2 = b.pixel_x + b.w / 2, b.pixel_y + b.h / 2
    ix1, iy1 = max(ax1, bx1), max(ay1, by1)
    ix2, iy2 = min(ax2, bx2), min(ay2, by2)
    iw, ih = max(0.0, ix2 - ix1), max(0.0, iy2 - iy1)
    inter = iw * ih
    if inter <= 0:
        return 0.0
    return inter / ((a.w * a.h) + (b.w * b.h) - inter)


def global_nms(dets: List[Detection], iou_thr: float = 0.45
               ) -> List[Detection]:
    """Simple O(n²) NMS — fine because we expect tens/hundreds per scene."""
    dets = sorted(dets, key=lambda d: d.conf, reverse=True)
    kept: List[Detection] = []
    for d in dets:
        if all(_iou(d, k) < iou_thr for k in kept if k.cls == d.cls):
            kept.append(d)
    return kept


# ────────────────────── inference ──────────────────────

def run_inference(safe: Path,
                  weights: Path,
                  db_path: os.PathLike = DEFAULT_DB_PATH,
                  pol: str = "vv",
                  tile: int = 800,
                  overlap: int = 80,
                  conf: float = 0.2,
                  iou_nms: float = 0.45,
                  product_id: Optional[str] = None,
                  workdir: Optional[Path] = None) -> List[Detection]:
    """
    Tile → predict → NMS → persist. Returns the final detection list.
    """
    try:
        from ultralytics import YOLO  # type: ignore
    except ImportError as e:
        raise SystemExit(
            "yolo_infer_sar.py needs: pip install ultralytics torch\n"
            f"Missing: {getattr(e, 'name', e)}") from e

    safe = Path(safe)
    weights = Path(weights)
    workdir = Path(workdir or tempfile.mkdtemp(prefix="nocturnal_tiles_"))
    print(f"[infer] scene={safe.name}")
    print(f"[infer] tiles -> {workdir}")

    # Step 1 — tile the whole scene (keep blank tiles for completeness)
    tile_paths = grd_to_tiles(safe, workdir, pol=pol,
                              tile=tile, overlap=overlap,
                              skip_blank=False)
    manifest = json.loads(next(workdir.glob("*_tiles.json")).read_text())
    tiles_idx = {t["file"]: t for t in manifest["tiles"]}
    print(f"[infer] {len(tile_paths)} tiles")

    # Step 2 — run the model over every tile
    model = YOLO(str(weights))
    results = model(
        source=[str(p) for p in tile_paths],
        conf=conf, iou=iou_nms,     # per-tile NMS too
        verbose=False,
    )

    # Step 3 — collect, translate to full-scene pixel coords
    raw: List[Detection] = []
    for result, tp in zip(results, tile_paths):
        meta = tiles_idx.get(tp.name)
        if meta is None or result.boxes is None:
            continue
        r0, c0 = meta["row"], meta["col"]
        xyxy = result.boxes.xyxy.cpu().numpy()   # (N, 4) in tile pixels
        cls  = result.boxes.cls.cpu().numpy().astype(int)
        cf   = result.boxes.conf.cpu().numpy()
        for (x1, y1, x2, y2), k, p in zip(xyxy, cls, cf):
            raw.append(Detection(
                pixel_x=c0 + (x1 + x2) / 2.0,
                pixel_y=r0 + (y1 + y2) / 2.0,
                w=float(x2 - x1), h=float(y2 - y1),
                conf=float(p), cls=int(k)))

    print(f"[infer] {len(raw)} raw detections (before global NMS)")
    final = global_nms(raw, iou_thr=iou_nms)
    print(f"[infer] {len(final)} final detections after global NMS")

    # Step 4 — persist
    ensure_detection_schema(db_path)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with sqlite3.connect(os.fspath(db_path), timeout=30) as c:
        c.executemany(
            """INSERT INTO detections
               (product_id, scene_name, polarisation,
                pixel_x, pixel_y, width_px, height_px,
                confidence, class_id, model_name, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            [(product_id, safe.name, pol,
              d.pixel_x, d.pixel_y, d.w, d.h,
              d.conf, d.cls, weights.name, now)
             for d in final])
        c.commit()
    print(f"[infer] wrote {len(final)} rows to {db_path}")
    return final


# ────────────────────── CLI ──────────────────────

def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("safe", type=Path,
                    help="path to an unzipped .SAFE directory")
    ap.add_argument("--weights", type=Path, required=True,
                    help="path to best.pt from Phase 3 training")
    ap.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    ap.add_argument("--pol", default="vv", choices=["vv", "vh"])
    ap.add_argument("--tile", type=int, default=800)
    ap.add_argument("--overlap", type=int, default=80)
    ap.add_argument("--conf", type=float, default=0.2,
                    help="detection confidence threshold")
    ap.add_argument("--iou",  type=float, default=0.45,
                    help="NMS IoU threshold (tile + global)")
    ap.add_argument("--product-id", default=None,
                    help="CDSE product UUID (optional; links to sentinel_products)")
    ap.add_argument("--workdir", type=Path, default=None,
                    help="where to write tile PNGs (default: temp dir)")
    args = ap.parse_args(argv)

    dets = run_inference(
        safe=args.safe, weights=args.weights, db_path=args.db,
        pol=args.pol, tile=args.tile, overlap=args.overlap,
        conf=args.conf, iou_nms=args.iou,
        product_id=args.product_id, workdir=args.workdir)
    print(f"[ok] {len(dets)} detections written")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
