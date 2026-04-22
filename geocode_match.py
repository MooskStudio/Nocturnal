#!/usr/bin/env python3
"""
geocode_match.py — NOCTURNAL Phase 4: pixel → GPS conversion and
AIS cross-referencing.

This is where detected ship pixels become geolocated vessels, and
where we decide which of those vessels are "dark" (not broadcasting
AIS at the moment the satellite passed overhead).

Flow, per scene:
  1. Load all detections for the scene (from Phase 3) that still have
     NULL lat/lon.
  2. Open the scene's .tiff, pull its GCPs, build a polynomial
     transform, and convert each detection's (pixel_y, pixel_x) into
     WGS84 (lon, lat).
  3. Look up the scene's acquisition time from sentinel_products.
  4. For each detection, query the `positions` R-tree for any AIS
     ping within `radius_km` of the detection AND within ±`dt_min`
     of the acquisition time. Pick the closest match.
  5. Write back: lat, lon, matched_mmsi, match_dist_m, dark.

A detection is `dark=1` if NO AIS ping passes the spatial+temporal
filter. This is the raw Dark Vessel candidate list — Phase 5 scores
and prioritises it.

Install:
    pip install rasterio  (already a Phase 3 dep)

CLI:
    python geocode_match.py --scene S1A_IW_GRDH_..._SAFE \\
        --safe /data/scenes/S1A_IW_GRDH_..._SAFE \\
        --radius-km 1.0 --dt-min 30
"""

from __future__ import annotations

import argparse
import math
import os
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Optional, Tuple

try:
    import rasterio
    from rasterio.transform import from_gcps
except ImportError as e:
    raise SystemExit(
        "geocode_match.py needs: pip install rasterio\n"
        f"Missing: {getattr(e, 'name', e)}") from e

# Phase 3 already depends on sar_preprocess; reuse its locator.
from sar_preprocess import locate_measurement   # noqa: E402


DEFAULT_DB_PATH = Path(__file__).resolve().parent / "ais_memory.db"


# ────────────────────── geodesy ──────────────────────

_EARTH_R_M = 6_371_008.8   # IUGG mean Earth radius, metres


def haversine_m(lat1: float, lon1: float,
                lat2: float, lon2: float) -> float:
    """
    Great-circle distance between two WGS84 points, in metres.
    Good enough at the spatial scales we care about (<10 km).
    """
    rlat1, rlat2 = math.radians(lat1), math.radians(lat2)
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (math.sin(dlat / 2) ** 2
         + math.cos(rlat1) * math.cos(rlat2) * math.sin(dlon / 2) ** 2)
    return 2 * _EARTH_R_M * math.asin(math.sqrt(a))


def _bbox_deg(lat: float, lon: float, radius_m: float
              ) -> Tuple[float, float, float, float]:
    """Cheap lat/lon box that encloses a radius-R disc around (lat,lon)."""
    dlat = radius_m / 111_320.0
    # cos clamp so we don't blow up near the poles (irrelevant for the
    # Channel, but cheap insurance).
    coslat = max(math.cos(math.radians(lat)), 1e-6)
    dlon = radius_m / (111_320.0 * coslat)
    return (lat - dlat, lat + dlat, lon - dlon, lon + dlon)


# ────────────────────── pixel → geo ──────────────────────

def _open_gcp_transform(safe: Path, pol: str):
    """
    Open the measurement .tiff and return (transform, crs).

    Sentinel-1 GRD .tiff files carry GCPs in their metadata (not a
    plain affine), so rasterio.transform.xy() against src.transform
    would be wrong. We build a polynomial transform from the GCPs
    instead; this is what rasterio's own warping path uses.
    """
    tif = locate_measurement(safe, pol)
    src = rasterio.open(tif)
    gcps, crs = src.gcps
    if not gcps:
        src.close()
        raise RuntimeError(
            f"No GCPs on {tif.name} — cannot geolocate. "
            "This is unusual for Sentinel-1 IW GRD; check the product.")
    transform = from_gcps(gcps)
    return src, transform, crs


def pixel_to_lonlat(transform, row: float, col: float
                    ) -> Tuple[float, float]:
    """
    Map a full-scene pixel (row, col) to world coords using a
    GCP-derived transform. Rasterio returns (x, y); for a GRD
    footprint that's (lon, lat) in EPSG:4326.
    """
    # Use centre-of-pixel convention (offset="center") to match how
    # detection centres are defined in Phase 3.
    x, y = rasterio.transform.xy(transform, row, col, offset="center")
    return float(x), float(y)


# ────────────────────── AIS match ──────────────────────

@dataclass
class PingMatch:
    mmsi: int
    dist_m: float
    dt_s: float
    ts_epoch: float
    lat: float
    lon: float


def _nearest_ping(conn: sqlite3.Connection,
                  lat: float, lon: float,
                  t_mid: float, dt_s: float,
                  radius_m: float) -> Optional[PingMatch]:
    """
    Use the R-tree bbox filter first (cheap, index-backed), then
    recompute the precise haversine distance on the shortlist and
    return the closest point that still satisfies both limits.
    """
    lat_lo, lat_hi, lon_lo, lon_hi = _bbox_deg(lat, lon, radius_m)
    q = """
        SELECT p.mmsi, p.ts_epoch, p.lat, p.lon
        FROM positions_rtree r
        JOIN positions p ON p.id = r.id
        WHERE r.min_lat >= ? AND r.max_lat <= ?
          AND r.min_lon >= ? AND r.max_lon <= ?
          AND p.ts_epoch BETWEEN ? AND ?
    """
    t_lo, t_hi = t_mid - dt_s, t_mid + dt_s
    rows = conn.execute(q, (lat_lo, lat_hi, lon_lo, lon_hi,
                            t_lo, t_hi)).fetchall()
    if not rows:
        return None

    best: Optional[PingMatch] = None
    for mmsi, tse, plat, plon in rows:
        d = haversine_m(lat, lon, plat, plon)
        if d > radius_m:
            continue   # bbox let in the corners of the square
        if best is None or d < best.dist_m:
            best = PingMatch(mmsi=int(mmsi), dist_m=float(d),
                             dt_s=float(tse - t_mid),
                             ts_epoch=float(tse),
                             lat=float(plat), lon=float(plon))
    return best


# ────────────────────── scene orchestration ──────────────────────

def _scene_start_epoch(conn: sqlite3.Connection,
                       scene_name: str) -> Optional[float]:
    row = conn.execute(
        "SELECT start_epoch FROM sentinel_products WHERE name = ?",
        (scene_name,)).fetchone()
    return float(row[0]) if row else None


def process_scene(scene_name: str,
                  safe: Path,
                  db_path: os.PathLike = DEFAULT_DB_PATH,
                  pol: Optional[str] = None,
                  radius_km: float = 1.0,
                  dt_min: float = 30.0,
                  dry_run: bool = False) -> dict:
    """
    Geocode and match every detection belonging to `scene_name`.
    Returns a small summary dict; also writes back to the database
    unless dry_run=True.
    """
    safe = Path(safe)
    radius_m = radius_km * 1000.0
    dt_s     = dt_min * 60.0

    conn = sqlite3.connect(os.fspath(db_path), timeout=30)
    try:
        # Pull unresolved detections for this scene (lat IS NULL means
        # Phase 4 hasn't touched it yet; idempotent re-runs).
        dets = conn.execute(
            """SELECT id, pixel_y, pixel_x, polarisation
               FROM detections
               WHERE scene_name = ? AND lat IS NULL""",
            (scene_name,)).fetchall()
        if not dets:
            return {"scene": scene_name, "detections": 0,
                    "reason": "nothing to do (no NULL-lat rows)"}

        # Acquisition time from the Phase 2 catalogue row.
        t_mid = _scene_start_epoch(conn, scene_name)
        if t_mid is None:
            # If the product wasn't registered via sentinel_fetch, fall
            # back to the SAFE folder's embedded start time. Keeping
            # this permissive makes the tool usable on ad-hoc scenes.
            t_mid = _start_epoch_from_safe(safe)

        pol_eff = pol or (dets[0][3] or "vv")
        src, transform, crs = _open_gcp_transform(safe, pol_eff)

        updates: List[tuple] = []
        n_dark = n_match = 0
        try:
            for det_id, py, px, _ in dets:
                lon, lat = pixel_to_lonlat(transform, py, px)
                m = _nearest_ping(conn, lat, lon, t_mid, dt_s, radius_m)
                if m is None:
                    n_dark += 1
                    updates.append((lat, lon, None, None, 1, det_id))
                else:
                    n_match += 1
                    updates.append((lat, lon, m.mmsi, m.dist_m, 0, det_id))
        finally:
            src.close()

        if dry_run:
            return {"scene": scene_name, "detections": len(dets),
                    "matched": n_match, "dark": n_dark,
                    "t_mid": t_mid, "dry_run": True}

        conn.executemany(
            """UPDATE detections
                  SET lat = ?, lon = ?,
                      matched_mmsi = ?, match_dist_m = ?, dark = ?
                WHERE id = ?""",
            updates)
        conn.commit()

        return {"scene": scene_name, "detections": len(dets),
                "matched": n_match, "dark": n_dark, "t_mid": t_mid}
    finally:
        conn.close()


def _start_epoch_from_safe(safe: Path) -> float:
    """
    Fallback when the scene wasn't registered in sentinel_products.
    Parses the SAFE folder name: the 4th underscore-delimited field
    is the acquisition start time in YYYYMMDDTHHMMSS form.
    Example: S1A_IW_GRDH_1SDV_20240115T061528_20240115T061553_...
    """
    from datetime import datetime, timezone
    parts = safe.name.split("_")
    # Find the first *T*-stamped part
    for p in parts:
        if len(p) >= 15 and p[8:9] == "T":
            dt = datetime.strptime(p[:15], "%Y%m%dT%H%M%S")
            return dt.replace(tzinfo=timezone.utc).timestamp()
    raise RuntimeError(f"cannot infer acquisition time from {safe.name}")


# ────────────────────── CLI ──────────────────────

def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--scene", required=True,
                    help="scene_name (SAFE folder name) as stored in detections")
    ap.add_argument("--safe", type=Path, required=True,
                    help="path to the unzipped .SAFE directory")
    ap.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    ap.add_argument("--pol", default=None, choices=["vv", "vh", None])
    ap.add_argument("--radius-km", type=float, default=1.0,
                    help="spatial match radius (default 1.0 km)")
    ap.add_argument("--dt-min", type=float, default=30.0,
                    help="temporal match half-window in minutes (default ±30)")
    ap.add_argument("--dry-run", action="store_true",
                    help="compute and report, but don't write back to DB")
    args = ap.parse_args(argv)

    summary = process_scene(
        scene_name=args.scene, safe=args.safe, db_path=args.db,
        pol=args.pol, radius_km=args.radius_km, dt_min=args.dt_min,
        dry_run=args.dry_run)
    print("[phase4]", summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
