#!/usr/bin/env python3
"""
ais_store.py — AIS persistence layer for NOCTURNAL (Phase 1: AIS Memory).

Purpose
-------
Live AIS streams are ephemeral — ais_proxy.py forwards messages to the
browser and forgets them instantly. Satellite imagery (Sentinel-1) only
catches past snapshots. To detect "Dark Vessels" we must be able to
answer, days later: "Was there any AIS ping within 1 km of this pixel
at the moment the satellite was overhead?"

This module gives the whole pipeline a shared memory:
  - A single SQLite database (stdlib sqlite3 — no native deps)
  - WAL journal mode (safe for concurrent readers while a writer streams)
  - R-tree index on (lat, lon) for fast spatial-range queries
  - Compound index on (mmsi, ts_epoch) for vessel histories
  - Upsert of vessel identity/dimensions into a separate `vessels` table

It plugs into both data sources already in the project:
  - ais_proxy.py         (aisstream.io WebSocket stream)
  - aishub_tracker.py    (AISHub REST poller)

Cross-platform
--------------
Pure stdlib. Works identically on macOS and Windows. The R-tree module
ships with CPython's bundled SQLite on both platforms.
"""

from __future__ import annotations

import json
import math
import os
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional


# ───────────────────────── schema ─────────────────────────
# PRAGMAs are applied per-connection in __init__ (they don't persist in
# schema scripts on first-open for new files).

_SCHEMA = """
CREATE TABLE IF NOT EXISTS positions (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    mmsi       INTEGER NOT NULL,
    ts_utc     TEXT    NOT NULL,       -- ISO-8601 UTC, sortable as text
    ts_epoch   REAL    NOT NULL,       -- UNIX seconds (cheap range queries)
    lat        REAL    NOT NULL,
    lon        REAL    NOT NULL,
    sog        REAL,                   -- speed over ground, knots
    cog        REAL,                   -- course over ground, deg
    heading    REAL,                   -- true heading, deg
    nav_status INTEGER,
    src        TEXT    NOT NULL,       -- 'aisstream' | 'aishub' | ...
    raw        TEXT                    -- original JSON payload (audit trail)
);

CREATE INDEX IF NOT EXISTS idx_positions_ts        ON positions(ts_epoch);
CREATE INDEX IF NOT EXISTS idx_positions_mmsi_ts   ON positions(mmsi, ts_epoch);

CREATE TABLE IF NOT EXISTS vessels (
    mmsi        INTEGER PRIMARY KEY,
    name        TEXT,
    callsign    TEXT,
    imo         INTEGER,
    ship_type   INTEGER,
    length_m    REAL,
    width_m     REAL,
    destination TEXT,
    first_seen  TEXT,
    last_seen   TEXT
);

-- Spatial index. Each row mirrors a positions.id with its (lat,lon) as a
-- degenerate 2D box. Enables WHERE min_lat<=?<=max_lat AND min_lon<=?<=max_lon.
CREATE VIRTUAL TABLE IF NOT EXISTS positions_rtree
USING rtree(id, min_lat, max_lat, min_lon, max_lon);
"""


class AISStore:
    """Thread-safe SQLite writer/reader for AIS records."""

    def __init__(self, db_path: str):
        self.db_path = os.fspath(db_path)
        self._lock = threading.Lock()
        # check_same_thread=False + our own lock => safe multi-thread use
        self._conn = sqlite3.connect(self.db_path,
                                     check_same_thread=False,
                                     timeout=30)
        self._conn.executescript(
            "PRAGMA journal_mode=WAL;"
            "PRAGMA synchronous=NORMAL;"
            "PRAGMA foreign_keys=ON;"
        )
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # ─────────────────────── helpers ───────────────────────

    @staticmethod
    def _now_iso() -> str:
        return datetime.now(timezone.utc).isoformat(timespec="seconds")

    @staticmethod
    def _to_epoch(iso: str) -> float:
        """Parse ISO-8601 (accept a few aisstream variants) → epoch seconds."""
        if not iso:
            return datetime.now(timezone.utc).timestamp()
        s = (iso.replace(" UTC", "")
                .replace(" +0000", "+00:00")
                .replace("Z", "+00:00"))
        # aisstream sometimes uses: '2025-01-15 12:34:56.789012 +0000'
        if " " in s and "T" not in s:
            s = s.replace(" ", "T", 1)
        try:
            return datetime.fromisoformat(s).timestamp()
        except ValueError:
            return datetime.now(timezone.utc).timestamp()

    # ───────────────── aisstream.io ingest ─────────────────

    def record_aisstream(self, msg: Dict[str, Any]) -> bool:
        """
        Record one aisstream.io message (PositionReport or ShipStaticData).

        Returns True if a position row was written.  Safe to call for
        any message type — unrelated types are silently skipped.
        """
        mtype = msg.get("MessageType")
        meta  = msg.get("MetaData") or {}
        mmsi  = meta.get("MMSI")
        if mmsi is None:
            return False

        ts_iso  = meta.get("time_utc") or self._now_iso()
        ts_epch = self._to_epoch(ts_iso)
        body    = msg.get("Message") or {}
        wrote   = False

        if mtype == "PositionReport":
            pr  = body.get("PositionReport") or {}
            lat = pr.get("Latitude")
            lon = pr.get("Longitude")
            if lat is None or lon is None:
                return False
            with self._lock:
                cur = self._conn.execute(
                    """INSERT INTO positions
                       (mmsi, ts_utc, ts_epoch, lat, lon,
                        sog, cog, heading, nav_status, src, raw)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                    (int(mmsi), ts_iso, ts_epch, float(lat), float(lon),
                     pr.get("Sog"), pr.get("Cog"), pr.get("TrueHeading"),
                     pr.get("NavigationalStatus"), "aisstream",
                     json.dumps(msg, ensure_ascii=False)))
                rowid = cur.lastrowid
                self._conn.execute(
                    "INSERT INTO positions_rtree VALUES (?,?,?,?,?)",
                    (rowid, float(lat), float(lat), float(lon), float(lon)))
                self._upsert_vessel_seen(int(mmsi), ts_iso,
                                         name=meta.get("ShipName"))
                self._conn.commit()
            wrote = True

        elif mtype == "ShipStaticData":
            ssd = body.get("ShipStaticData") or {}
            with self._lock:
                self._upsert_vessel_static(int(mmsi), ts_iso,
                                           meta.get("ShipName"), ssd)
                self._conn.commit()

        return wrote

    # ──────────────── AISHub snapshot ingest ────────────────

    def record_aishub_snapshot(self,
                               vessels: Iterable[Dict[str, Any]],
                               poll_ts_iso: str) -> int:
        """
        Record one AISHub poll snapshot. Vessel dicts use AISHub's
        upper-case keys (MMSI, LATITUDE, LONGITUDE, SOG, COG, ...).

        Returns the number of position rows written.
        """
        ts_epch = self._to_epoch(poll_ts_iso)
        written = 0
        with self._lock:
            for v in vessels:
                mmsi = v.get("MMSI")
                lat  = v.get("LATITUDE")
                lon  = v.get("LONGITUDE")
                if mmsi is None or lat is None or lon is None:
                    continue
                cur = self._conn.execute(
                    """INSERT INTO positions
                       (mmsi, ts_utc, ts_epoch, lat, lon,
                        sog, cog, heading, nav_status, src, raw)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                    (int(mmsi), poll_ts_iso, ts_epch,
                     float(lat), float(lon),
                     v.get("SOG"), v.get("COG"), v.get("HEADING"),
                     v.get("NAVSTAT"), "aishub",
                     json.dumps(v, ensure_ascii=False)))
                rowid = cur.lastrowid
                self._conn.execute(
                    "INSERT INTO positions_rtree VALUES (?,?,?,?,?)",
                    (rowid, float(lat), float(lat),
                     float(lon), float(lon)))
                self._upsert_vessel_static(int(mmsi), poll_ts_iso,
                                           v.get("NAME"), v)
                written += 1
            self._conn.commit()
        return written

    # ─────────────── vessel metadata upserts ───────────────

    def _upsert_vessel_seen(self, mmsi: int, ts_iso: str,
                            name: Optional[str] = None) -> None:
        self._conn.execute(
            """INSERT INTO vessels (mmsi, name, first_seen, last_seen)
               VALUES (?,?,?,?)
               ON CONFLICT(mmsi) DO UPDATE SET
                 last_seen = excluded.last_seen,
                 name      = COALESCE(NULLIF(excluded.name,''), vessels.name)""",
            (mmsi, (name or "").strip() or None, ts_iso, ts_iso))

    def _upsert_vessel_static(self, mmsi: int, ts_iso: str,
                              name: Optional[str],
                              data: Dict[str, Any]) -> None:
        """Upsert identity / dimensions from a static-data message."""
        def pick(*keys):
            for k in keys:
                if k in data and data[k] not in ("", None):
                    return data[k]
            return None

        # Dimensions: aisstream uses Dimension.{A,B,C,D}; AISHub uses A,B,C,D.
        dim = data.get("Dimension") or {}
        def dim_pick(field, alt):
            v = dim.get(field)
            if v in ("", None):
                v = data.get(alt)
            return v if isinstance(v, (int, float)) else None

        a = dim_pick("A", "A")
        b = dim_pick("B", "B")
        c = dim_pick("C", "C")
        d = dim_pick("D", "D")
        length = (a + b) if a is not None and b is not None else None
        width  = (c + d) if c is not None and d is not None else None

        self._conn.execute(
            """INSERT INTO vessels (mmsi, name, callsign, imo, ship_type,
                                    length_m, width_m, destination,
                                    first_seen, last_seen)
               VALUES (?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(mmsi) DO UPDATE SET
                 name        = COALESCE(NULLIF(excluded.name,''),      vessels.name),
                 callsign    = COALESCE(NULLIF(excluded.callsign,''),  vessels.callsign),
                 imo         = COALESCE(excluded.imo,         vessels.imo),
                 ship_type   = COALESCE(excluded.ship_type,   vessels.ship_type),
                 length_m    = COALESCE(excluded.length_m,    vessels.length_m),
                 width_m     = COALESCE(excluded.width_m,     vessels.width_m),
                 destination = COALESCE(NULLIF(excluded.destination,''),vessels.destination),
                 last_seen   = excluded.last_seen""",
            (mmsi,
             (name or pick("Name", "NAME") or "").strip() or None,
             (pick("CallSign", "CALLSIGN") or "").strip() or None,
             pick("ImoNumber", "IMO"),
             pick("Type", "TYPE"),
             length, width,
             (pick("Destination", "DEST") or "").strip() or None,
             ts_iso, ts_iso))

    # ─────────────────────── queries ───────────────────────

    def pings_within(self, lat: float, lon: float,
                     start_ts: float, end_ts: float,
                     radius_km: float = 1.0) -> List[sqlite3.Row]:
        """
        Positions within `radius_km` of (lat, lon) in [start_ts, end_ts].

        This is the core Phase 4 query: given a satellite-detected blob,
        was *any* vessel broadcasting AIS nearby at the overpass time?
        """
        # Pre-filter with R-tree using a degree-based bounding box; then
        # refine with Haversine. In the Channel (≈50°N), 1° lat ≈ 111 km,
        # 1° lon ≈ 111·cos(50°) ≈ 71 km, so we widen lon deliberately.
        d_lat = radius_km / 111.0
        d_lon = radius_km / (111.0 * max(0.1, math.cos(math.radians(lat))))

        self._conn.row_factory = sqlite3.Row
        q = """
          SELECT p.* FROM positions_rtree r
          JOIN positions p ON p.id = r.id
          WHERE r.min_lat >= ? AND r.max_lat <= ?
            AND r.min_lon >= ? AND r.max_lon <= ?
            AND p.ts_epoch BETWEEN ? AND ?
        """
        with self._lock:
            rows = self._conn.execute(
                q,
                (lat - d_lat, lat + d_lat,
                 lon - d_lon, lon + d_lon,
                 start_ts, end_ts)).fetchall()

        return [r for r in rows
                if _haversine_km(lat, lon, r["lat"], r["lon"]) <= radius_km]

    def stats(self) -> Dict[str, int]:
        with self._lock:
            pos = self._conn.execute(
                "SELECT COUNT(*) FROM positions").fetchone()[0]
            ves = self._conn.execute(
                "SELECT COUNT(*) FROM vessels").fetchone()[0]
        return {"positions": pos, "vessels": ves}


def _haversine_km(lat1: float, lon1: float,
                  lat2: float, lon2: float) -> float:
    R = 6371.0088  # mean Earth radius, km
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = (math.sin(dp / 2) ** 2
         + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2)
    return 2 * R * math.asin(math.sqrt(a))


# ───────────────────── CLI smoke test ─────────────────────

if __name__ == "__main__":
    import sys, pathlib
    db = sys.argv[1] if len(sys.argv) > 1 else "ais_memory.db"
    with AISStore(db) as store:
        print(f"[ok] opened {pathlib.Path(db).resolve()}")
        print(f"[stats] {store.stats()}")
