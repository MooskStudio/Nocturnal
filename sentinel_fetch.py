#!/usr/bin/env python3
"""
sentinel_fetch.py — NOCTURNAL Phase 2: automated Sentinel-1 GRD fetching.

Queries the Copernicus Data Space Ecosystem (CDSE) OData catalogue for
Sentinel-1 IW GRD products intersecting the English Channel bounding box
and downloads them to a local cache folder. Every fetched product is
registered in a `sentinel_products` table inside the same SQLite database
used by Phase 1, so the detector (Phase 3) and the matcher (Phase 4) can
join against AIS pings by footprint + timestamp.

Cross-platform. Pure Python; requires `requests` only.

CLI
---
  # list available IW GRD products over the last 2 days, no download
  python sentinel_fetch.py list --since 2d

  # download the latest product intersecting the English Channel
  python sentinel_fetch.py fetch --since 1d --limit 1

Credentials
-----------
Register (free) at https://dataspace.copernicus.eu/ and export:
  macOS / Linux
    export CDSE_USER="you@example.com"
    export CDSE_PASS="your-password"
  Windows (PowerShell)
    $env:CDSE_USER = "you@example.com"
    $env:CDSE_PASS = "your-password"
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import threading
import time
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

try:
    import requests  # type: ignore
except ImportError as _e:  # pragma: no cover
    raise SystemExit(
        "sentinel_fetch.py requires the 'requests' package.\n"
        "Install it with:  pip install requests"
    ) from _e


# ─────────────────────── constants ───────────────────────

CDSE_AUTH_URL     = "https://identity.dataspace.copernicus.eu/auth/realms/CDSE/protocol/openid-connect/token"
CDSE_ODATA_URL    = "https://catalogue.dataspace.copernicus.eu/odata/v1/Products"
CDSE_DOWNLOAD_TPL = "https://download.dataspace.copernicus.eu/odata/v1/Products({id})/$value"

# English Channel bbox — must match ais_proxy.py / aishub_tracker.py
BBOX_CHANNEL: Tuple[float, float, float, float] = (-6.0, 48.3, 2.5, 51.5)
#                                                   min_lon, min_lat, max_lon, max_lat

# Default cache location: <script dir>/sentinel_data
DEFAULT_DATA_DIR = Path(__file__).resolve().parent / "sentinel_data"
DEFAULT_DB_PATH  = Path(__file__).resolve().parent / "ais_memory.db"


# ─────────────────────── schema ───────────────────────

_SCHEMA_SENTINEL = """
CREATE TABLE IF NOT EXISTS sentinel_products (
    product_id     TEXT PRIMARY KEY,    -- CDSE UUID
    name           TEXT NOT NULL,       -- SAFE folder name
    collection     TEXT,
    product_type   TEXT,                -- GRDH, GRDM, SLC, ...
    mode           TEXT,                -- IW, EW, SM, WV
    polarisation   TEXT,                -- DV (VV+VH), DH, SV, SH
    orbit_dir      TEXT,                -- ASCENDING | DESCENDING
    relative_orbit INTEGER,
    start_utc      TEXT NOT NULL,       -- ISO-8601
    start_epoch    REAL NOT NULL,
    end_utc        TEXT,
    end_epoch      REAL,
    footprint_wkt  TEXT,
    size_bytes     INTEGER,
    file_path      TEXT,                -- local path once downloaded, else NULL
    downloaded_at  TEXT,
    registered_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_s1_start   ON sentinel_products(start_epoch);
CREATE INDEX IF NOT EXISTS idx_s1_file    ON sentinel_products(file_path);
"""


def ensure_sentinel_schema(db_path: str | os.PathLike) -> None:
    """Create the sentinel_products table if missing (idempotent)."""
    with sqlite3.connect(os.fspath(db_path), timeout=30) as c:
        c.executescript(
            "PRAGMA journal_mode=WAL;"
            "PRAGMA synchronous=NORMAL;"
        )
        c.executescript(_SCHEMA_SENTINEL)
        c.commit()


# ─────────────────────── time helpers ───────────────────────

def parse_since(spec: str) -> datetime:
    """
    Convert a human shorthand like '3d', '12h', '2026-04-15' or ISO-8601
    into a UTC datetime.
    """
    spec = spec.strip()
    now = datetime.now(timezone.utc)
    if spec.endswith(("d", "h", "m")):
        n = int(spec[:-1])
        unit = spec[-1]
        delta = {"d": timedelta(days=n),
                 "h": timedelta(hours=n),
                 "m": timedelta(minutes=n)}[unit]
        return now - delta
    # Try ISO-8601 or date
    s = spec.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError as e:
        raise SystemExit(f"Cannot parse --since value: {spec!r}") from e
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def iso_z(dt: datetime) -> str:
    """Render a datetime as OData-compatible '...Z'."""
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def bbox_wkt(min_lon: float, min_lat: float,
             max_lon: float, max_lat: float) -> str:
    """OGC WKT polygon for a lon/lat bounding box (counter-clockwise)."""
    return (
        "POLYGON(("
        f"{min_lon} {min_lat}, "
        f"{max_lon} {min_lat}, "
        f"{max_lon} {max_lat}, "
        f"{min_lon} {max_lat}, "
        f"{min_lon} {min_lat}"
        "))"
    )


# ─────────────────────── CDSE client ───────────────────────

@dataclass
class Product:
    """A slimmed-down view of a CDSE product row."""
    product_id:     str
    name:           str
    collection:     Optional[str]
    product_type:   Optional[str]
    mode:           Optional[str]
    polarisation:   Optional[str]
    orbit_dir:      Optional[str]
    relative_orbit: Optional[int]
    start_utc:      str
    end_utc:        Optional[str]
    footprint_wkt:  Optional[str]
    size_bytes:     Optional[int]

    @classmethod
    def from_odata(cls, row: Dict[str, Any]) -> "Product":
        # Attributes live in row["Attributes"] as a list of {Name, Value, ValueType}
        attrs = {a.get("Name"): a.get("Value")
                 for a in (row.get("Attributes") or [])}

        def _int(x):
            try: return int(x) if x is not None else None
            except (TypeError, ValueError): return None

        footprint = None
        geofp = row.get("GeoFootprint") or {}
        # CDSE returns GeoJSON-like; we prefer the Footprint attribute (WKT)
        if "Footprint" in attrs and isinstance(attrs["Footprint"], str):
            footprint = attrs["Footprint"].replace("geography'SRID=4326;", "").rstrip("'")
        elif geofp:
            footprint = json.dumps(geofp)

        return cls(
            product_id     = row.get("Id"),
            name           = row.get("Name", ""),
            collection     = (row.get("Collection") or {}).get("Name")
                             if isinstance(row.get("Collection"), dict) else None,
            product_type   = attrs.get("productType"),
            mode           = attrs.get("operationalMode") or attrs.get("sensorMode"),
            polarisation   = attrs.get("polarisationChannels") or attrs.get("polarisation"),
            orbit_dir      = attrs.get("orbitDirection"),
            relative_orbit = _int(attrs.get("relativeOrbitNumber")),
            start_utc      = (row.get("ContentDate") or {}).get("Start"),
            end_utc        = (row.get("ContentDate") or {}).get("End"),
            footprint_wkt  = footprint,
            size_bytes     = _int(row.get("ContentLength")),
        )


class CDSEClient:
    """Thin Copernicus Data Space client: search + download."""

    def __init__(self,
                 username: Optional[str] = None,
                 password: Optional[str] = None,
                 session: Optional["requests.Session"] = None):
        self.username = username or os.environ.get("CDSE_USER") or ""
        self.password = password or os.environ.get("CDSE_PASS") or ""
        self._token:     Optional[str] = None
        self._token_exp: float = 0.0
        self._s = session or requests.Session()
        self._s.headers.update({"User-Agent": "NOCTURNAL/phase2"})
        self._auth_lock = threading.Lock()

    # ── auth ──────────────────────────────────────────────

    def _require_creds(self):
        if not self.username or not self.password:
            raise SystemExit(
                "Missing CDSE credentials. Set CDSE_USER and CDSE_PASS "
                "environment variables (free registration at "
                "https://dataspace.copernicus.eu)."
            )

    def ensure_token(self) -> str:
        with self._auth_lock:
            if self._token and time.time() < self._token_exp - 30:
                return self._token
            self._require_creds()
            r = self._s.post(CDSE_AUTH_URL, data={
                "grant_type": "password",
                "username":   self.username,
                "password":   self.password,
                "client_id":  "cdse-public",
            }, timeout=30)
            if r.status_code != 200:
                raise SystemExit(
                    f"CDSE auth failed ({r.status_code}): {r.text[:200]}"
                )
            j = r.json()
            self._token     = j["access_token"]
            self._token_exp = time.time() + int(j.get("expires_in", 600))
            return self._token

    # ── search ────────────────────────────────────────────

    def search_grd(self,
                   bbox: Tuple[float, float, float, float] = BBOX_CHANNEL,
                   start: Optional[datetime] = None,
                   end:   Optional[datetime] = None,
                   mode:   str = "IW",
                   max_items: int = 50,
                   orbit_dir: Optional[str] = None) -> List[Product]:
        """
        Catalogue search. No authentication required for listing.
        """
        end   = end   or datetime.now(timezone.utc)
        start = start or (end - timedelta(days=7))
        wkt   = bbox_wkt(*bbox)

        clauses = [
            "Collection/Name eq 'SENTINEL-1'",
            "contains(Name,'GRD')",
            f"contains(Name,'_{mode}_')",
            f"OData.CSC.Intersects(area=geography'SRID=4326;{wkt}')",
            f"ContentDate/Start ge {iso_z(start)}",
            f"ContentDate/Start le {iso_z(end)}",
        ]
        if orbit_dir:
            clauses.append(
                "Attributes/OData.CSC.StringAttribute/any("
                "att:att/Name eq 'orbitDirection' "
                f"and att/OData.CSC.StringAttribute/Value eq '{orbit_dir.upper()}')"
            )

        params = {
            "$filter":  " and ".join(clauses),
            "$top":     str(max_items),
            "$orderby": "ContentDate/Start desc",
            "$expand":  "Attributes",
        }
        r = self._s.get(CDSE_ODATA_URL, params=params, timeout=60)
        if r.status_code != 200:
            raise SystemExit(
                f"CDSE search failed ({r.status_code}): {r.text[:200]}"
            )
        return [Product.from_odata(row) for row in r.json().get("value", [])]

    # ── download ──────────────────────────────────────────

    def download(self,
                 product: Product,
                 out_dir: Path,
                 chunk: int = 8 * 1024 * 1024,
                 on_progress=None) -> Path:
        """
        Stream the product's .SAFE.zip to `out_dir/<name>.zip`.
        Supports HTTP Range resume if the target file already exists.
        """
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        target = out_dir / f"{product.name}.zip"

        token = self.ensure_token()
        url = CDSE_DOWNLOAD_TPL.format(id=product.product_id)

        existing = target.stat().st_size if target.exists() else 0
        headers = {"Authorization": f"Bearer {token}"}
        mode = "wb"
        if existing and product.size_bytes and existing < product.size_bytes:
            headers["Range"] = f"bytes={existing}-"
            mode = "ab"
        elif existing and product.size_bytes and existing >= product.size_bytes:
            return target  # already complete

        with self._s.get(url, headers=headers, stream=True,
                         timeout=600, allow_redirects=True) as r:
            if r.status_code not in (200, 206):
                raise SystemExit(
                    f"Download failed ({r.status_code}): {r.text[:200]}"
                )
            total = existing + int(r.headers.get("Content-Length", "0") or 0)
            got   = existing
            with open(target, mode) as f:
                for block in r.iter_content(chunk_size=chunk):
                    if not block:
                        continue
                    f.write(block)
                    got += len(block)
                    if on_progress:
                        on_progress(got, total)
        return target


# ─────────────────────── registry ───────────────────────

def register_product(db_path: str | os.PathLike,
                     product: Product,
                     file_path: Optional[Path] = None) -> None:
    """Upsert a single product row."""
    ensure_sentinel_schema(db_path)
    def _epoch(iso: Optional[str]) -> Optional[float]:
        if not iso:
            return None
        try:
            return datetime.fromisoformat(
                iso.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None

    row = (
        product.product_id,
        product.name,
        product.collection,
        product.product_type,
        product.mode,
        product.polarisation,
        product.orbit_dir,
        product.relative_orbit,
        product.start_utc,
        _epoch(product.start_utc),
        product.end_utc,
        _epoch(product.end_utc),
        product.footprint_wkt,
        product.size_bytes,
        str(file_path) if file_path else None,
        datetime.now(timezone.utc).isoformat(timespec="seconds")
            if file_path else None,
        datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )
    with sqlite3.connect(os.fspath(db_path), timeout=30) as c:
        c.execute(
            """INSERT INTO sentinel_products (
                 product_id, name, collection, product_type, mode, polarisation,
                 orbit_dir, relative_orbit, start_utc, start_epoch,
                 end_utc, end_epoch, footprint_wkt, size_bytes,
                 file_path, downloaded_at, registered_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(product_id) DO UPDATE SET
                 file_path     = COALESCE(excluded.file_path,   sentinel_products.file_path),
                 downloaded_at = COALESCE(excluded.downloaded_at, sentinel_products.downloaded_at),
                 size_bytes    = COALESCE(excluded.size_bytes,  sentinel_products.size_bytes)""",
            row)
        c.commit()


# ─────────────────────── CLI ───────────────────────

def _fmt_bytes(n: Optional[int]) -> str:
    if not n:
        return "?"
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:7.1f} {unit}"
        n /= 1024
    return f"{n} B"


def _print_table(products: List[Product]) -> None:
    if not products:
        print("(no products)")
        return
    print(f"{'start (UTC)':20}  {'mode':4}  {'orb':3}  "
          f"{'pol':5}  {'size':>10}  name")
    print("-" * 110)
    for p in products:
        print(
            f"{(p.start_utc or '')[:19]:20}  "
            f"{(p.mode or '-'):4}  "
            f"{(p.orbit_dir or '?')[0:3]:3}  "
            f"{(p.polarisation or '-')[:5]:5}  "
            f"{_fmt_bytes(p.size_bytes):>10}  "
            f"{p.name}"
        )


def _progress_printer(last=[0.0]):
    def on_progress(got, total):
        now = time.time()
        if now - last[0] < 1.0 and got != total:
            return
        last[0] = now
        pct = (100.0 * got / total) if total else 0.0
        sys.stderr.write(
            f"\r    {_fmt_bytes(got)} / {_fmt_bytes(total)} "
            f"({pct:5.1f}%)"
        )
        sys.stderr.flush()
        if got == total:
            sys.stderr.write("\n")
    return on_progress


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description="NOCTURNAL Sentinel-1 GRD fetcher (CDSE).")
    sub = ap.add_subparsers(dest="cmd", required=True)

    for name, desc in (("list",  "search & print candidate products"),
                       ("fetch", "search & download products")):
        p = sub.add_parser(name, help=desc)
        p.add_argument("--since", default="2d",
                       help="shorthand (e.g. 3d, 12h) or ISO date. "
                            "Start of search window. Default 2d.")
        p.add_argument("--until", default=None,
                       help="ISO date; default now.")
        p.add_argument("--mode",  default="IW",
                       help="Sensor mode filter: IW (default) | EW | SM | WV.")
        p.add_argument("--orbit", default=None, choices=[None, "ASCENDING", "DESCENDING"],
                       help="Optional orbit direction filter.")
        p.add_argument("--bbox", nargs=4, type=float, metavar=("MINLON","MINLAT","MAXLON","MAXLAT"),
                       default=list(BBOX_CHANNEL),
                       help="Override the search bbox. Default: English Channel.")
        p.add_argument("--limit", type=int, default=50,
                       help="Max products to list/fetch.")
        p.add_argument("--db", default=str(DEFAULT_DB_PATH),
                       help="SQLite DB for the product registry. "
                            "Default: ais_memory.db next to this script.")
        if name == "fetch":
            p.add_argument("--out", default=str(DEFAULT_DATA_DIR),
                           help="Download directory. Default: ./sentinel_data")
            p.add_argument("--dry-run", action="store_true",
                           help="Print what would be downloaded, don't transfer.")

    args = ap.parse_args(argv)

    start = parse_since(args.since)
    end   = parse_since(args.until) if args.until else datetime.now(timezone.utc)
    bbox  = tuple(args.bbox)  # type: ignore

    # Registry always initialises, even on `list`.
    ensure_sentinel_schema(args.db)

    client = CDSEClient()
    print(f"[cdse] search bbox={bbox}  "
          f"{start.isoformat(timespec='minutes')} … {end.isoformat(timespec='minutes')}  "
          f"mode={args.mode}  orbit={args.orbit or 'any'}")
    products = client.search_grd(bbox=bbox, start=start, end=end,
                                 mode=args.mode, orbit_dir=args.orbit,
                                 max_items=args.limit)
    _print_table(products)

    if args.cmd == "list":
        return 0

    # fetch
    out_dir = Path(args.out)
    total_bytes = sum((p.size_bytes or 0) for p in products)
    print(f"\n[cdse] {len(products)} product(s) — ~{_fmt_bytes(total_bytes)} total")
    if args.dry_run:
        print("[cdse] --dry-run: no downloads started.")
        for p in products:
            register_product(args.db, p, file_path=None)
        print(f"[cdse] registered {len(products)} product(s) in "
              f"{args.db}")
        return 0

    for i, p in enumerate(products, 1):
        print(f"\n[{i}/{len(products)}] {p.name}  ({_fmt_bytes(p.size_bytes)})")
        try:
            path = client.download(p, out_dir, on_progress=_progress_printer())
            register_product(args.db, p, file_path=path)
            print(f"    -> {path}")
        except SystemExit as e:
            # Don't kill the whole batch for a single failure.
            print(f"    [error] {e}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
