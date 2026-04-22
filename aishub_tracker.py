#!/usr/bin/env python3
"""
AISHub English Channel Tracker
Polls AISHub REST API (max once/minute), displays on an interactive map,
and records vessel data to JSONL + CSV files.

Usage:
  python3 aishub_tracker.py

Then open http://localhost:8082 in your browser.
"""

import json
import time
import threading
import csv
import io
import os
import sys
import gzip
import fcntl
import urllib.request
import urllib.parse
from datetime import datetime, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler

from ais_store import AISStore

# ── Configuration ──────────────────────────────────────────────────────────────
API_KEY       = "AH_2670_9F0D1564"
API_URL       = "https://data.aishub.net/ws.php"
POLL_INTERVAL = 60          # seconds — DO NOT reduce below 60
PORT          = 8082
DATA_DIR       = "ais_data"  # directory for recorded files
# Shared across ALL scripts using this API key — lives in ~ so both trackers see it
LAST_POLL_FILE = os.path.expanduser("~/.aishub_last_poll")

# English Channel bounding box
LAT_MIN, LAT_MAX =  48.3, 51.5
LON_MIN, LON_MAX =  -6.0,  2.5

# ── AIS memory (Phase 1) ───────────────────────────────────────────────────────
DB_PATH = os.environ.get(
    "NOCTURNAL_DB",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "ais_memory.db"),
)
STORE = AISStore(DB_PATH)

# ── Shared state ───────────────────────────────────────────────────────────────
_state = {
    "vessels":       [],         # current snapshot
    "last_update":   None,       # ISO timestamp of last successful poll
    "next_poll_at":  None,       # ISO timestamp of next scheduled poll
    "poll_count":    0,
    "error":         None,
    "recording":     True,
    "total_records": 0,          # cumulative vessel-position records saved
    "session_file":  None,       # current JSONL file path
    "csv_file":      None,       # current CSV file path
}
_lock = threading.Lock()

# ── Polling ────────────────────────────────────────────────────────────────────

def _fetch_raw():
    """Call AISHub API and return parsed JSON."""
    params = urllib.parse.urlencode({
        "username": API_KEY,
        "format":   1,          # extended AIS format
        "output":   "json",
        "compress": 0,
        "latmin":   LAT_MIN,
        "latmax":   LAT_MAX,
        "lonmin":   LON_MIN,
        "lonmax":   LON_MAX,
    })
    url = f"{API_URL}?{params}"
    req = urllib.request.Request(url, headers={"User-Agent": "AISHubTracker/1.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        raw = resp.read()
        if resp.info().get("Content-Encoding") == "gzip":
            raw = gzip.decompress(raw)
        return json.loads(raw.decode("utf-8"))


def _ensure_data_dir():
    os.makedirs(DATA_DIR, exist_ok=True)


def _wait_for_poll_slot() -> None:
    """
    Block until it is safe to call the AISHub API, then atomically record the
    call time.  Uses an exclusive file lock (fcntl.flock) so that multiple
    scripts sharing the same API key (e.g. aishub_tracker + rnli_tracker
    running concurrently) cannot both fire within the same 60-second window.
    """
    while True:
        remaining = float(POLL_INTERVAL)
        try:
            with open(LAST_POLL_FILE, "a+") as fh:
                fcntl.flock(fh, fcntl.LOCK_EX)      # blocks if another script holds it
                fh.seek(0)
                content = fh.read().strip()
                last_ts = float(json.loads(content).get("ts", 0)) if content else 0
                elapsed = time.time() - last_ts

                if elapsed >= POLL_INTERVAL:
                    # Slot is free — claim it by writing our timestamp before releasing
                    fh.seek(0); fh.truncate()
                    json.dump({"ts": time.time()}, fh)
                    fh.flush()
                    return          # context manager releases the lock
                remaining = max(1.0, POLL_INTERVAL - elapsed)
                # context manager releases lock; we did NOT claim the slot
        except Exception:
            pass

        # Update UI countdown while we wait
        with _lock:
            _state["next_poll_at"] = datetime.fromtimestamp(
                time.time() + remaining, tz=timezone.utc
            ).isoformat()
        time.sleep(min(remaining, 5))   # re-check every 5 s at most


def _open_session_files():
    """Create new JSONL + CSV files for this session."""
    _ensure_data_dir()
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    jsonl = os.path.join(DATA_DIR, f"ais_{stamp}.jsonl")
    csv_f = os.path.join(DATA_DIR, f"ais_{stamp}.csv")
    # Write CSV header
    with open(csv_f, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow([
            "poll_timestamp", "MMSI", "NAME", "CALLSIGN", "TYPE",
            "LATITUDE", "LONGITUDE", "SOG", "COG", "HEADING",
            "NAVSTAT", "ROT", "IMO", "DRAUGHT", "DEST", "ETA",
            "A", "B", "C", "D",
        ])
    return jsonl, csv_f


def _append_record(timestamp: str, vessels: list, meta: dict,
                   jsonl_path: str, csv_path: str):
    """Append one poll snapshot to the JSONL file and rows to CSV."""
    # JSONL — full fidelity
    with open(jsonl_path, "a", encoding="utf-8") as f:
        f.write(json.dumps({"timestamp": timestamp, "meta": meta,
                             "vessels": vessels}) + "\n")

    # CSV — flattened rows
    with open(csv_path, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        for v in vessels:
            w.writerow([
                timestamp,
                v.get("MMSI", ""),    v.get("NAME", ""),
                v.get("CALLSIGN", ""),v.get("TYPE", ""),
                v.get("LATITUDE", ""),v.get("LONGITUDE", ""),
                v.get("SOG", ""),     v.get("COG", ""),
                v.get("HEADING", ""), v.get("NAVSTAT", ""),
                v.get("ROT", ""),     v.get("IMO", ""),
                v.get("DRAUGHT", ""), v.get("DEST", ""),
                v.get("ETA", ""),     v.get("A", ""),
                v.get("B", ""),       v.get("C", ""),
                v.get("D", ""),
            ])


def _poll_loop():
    global _state

    jsonl_path, csv_path = _open_session_files()
    with _lock:
        _state["session_file"] = jsonl_path
        _state["csv_file"]     = csv_path

    _ts = lambda: datetime.now().strftime("%H:%M:%S")

    while True:
        _wait_for_poll_slot()   # blocks until safe; handles cooldown + concurrent scripts
        poll_start = time.monotonic()
        try:
            print(f"[{_ts()}] Polling AISHub API…")
            data = _fetch_raw()

            meta    = data[0] if isinstance(data[0], dict) else {}
            vessels = data[1] if len(data) > 1 else []

            # AISHub wraps the error state in meta
            if meta.get("ERROR"):
                raise RuntimeError(f"API error: {meta}")

            timestamp = datetime.now(timezone.utc).isoformat()
            next_at   = datetime.fromtimestamp(
                time.time() + POLL_INTERVAL, tz=timezone.utc
            ).isoformat()

            with _lock:
                _state["vessels"]      = vessels
                _state["last_update"]  = timestamp
                _state["next_poll_at"] = next_at
                _state["poll_count"]  += 1
                _state["error"]        = None

                if _state["recording"]:
                    _append_record(timestamp, vessels, meta,
                                   jsonl_path, csv_path)
                    _state["total_records"] += len(vessels)

            # ── Phase 1: persist to SQLite ─────────────────────────────────
            written = STORE.record_aishub_snapshot(vessels, timestamp)
            db_stats = STORE.stats()

            print(f"  ↳ {len(vessels)} vessels  |  poll #{_state['poll_count']}"
                  f"  |  db: {db_stats['positions']} pings, {db_stats['vessels']} vessels")

        except Exception as exc:
            err = f"{type(exc).__name__}: {exc}"
            with _lock:
                _state["error"] = err
            print(f"  [ERROR] {err}", file=sys.stderr)

        elapsed = time.monotonic() - poll_start
        wait    = max(0, POLL_INTERVAL - elapsed)
        time.sleep(wait)


# ── HTTP server ────────────────────────────────────────────────────────────────

class _Server(HTTPServer):
    """HTTPServer with SO_REUSEADDR so the port is freed immediately on restart."""
    allow_reuse_address = True


class _Handler(BaseHTTPRequestHandler):

    def do_OPTIONS(self):
        self.send_response(200)
        self._cors()
        self.end_headers()

    def do_GET(self):
        path = self.path.split("?")[0].rstrip("/")

        if path in ("", "/"):
            self._serve_html()
        elif path == "/api/status":
            self._api_status()
        elif path == "/api/vessels":
            self._api_vessels()
        elif path == "/api/history":
            self._api_history()
        elif path == "/api/export/csv":
            self._api_export_csv()
        elif path == "/api/export/json":
            self._api_export_json()
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        path = self.path.split("?")[0].rstrip("/")

        if path == "/api/recording/start":
            with _lock:
                _state["recording"] = True
            self._json({"recording": True, "ok": True})

        elif path == "/api/recording/stop":
            with _lock:
                _state["recording"] = False
            self._json({"recording": False, "ok": True})

        else:
            self.send_response(404)
            self.end_headers()

    # ── response helpers ──

    def _json(self, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", len(body))
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin",  "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def log_message(self, fmt, *args):
        pass  # suppress request noise; polling thread prints its own status

    # ── API endpoints ──

    def _api_status(self):
        with _lock:
            snap = dict(_state)
            snap["vessel_count"] = len(snap.pop("vessels"))
        snap["poll_interval"] = POLL_INTERVAL
        self._json(snap)

    def _api_vessels(self):
        with _lock:
            payload = {
                "vessels":      _state["vessels"],
                "last_update":  _state["last_update"],
                "next_poll_at": _state["next_poll_at"],
                "poll_count":   _state["poll_count"],
                "error":        _state["error"],
            }
        self._json(payload)

    def _api_history(self):
        """Return a summary of all recorded polls (timestamp + vessel count)."""
        with _lock:
            path = _state["session_file"]
        if not path or not os.path.exists(path):
            self._json([])
            return
        summary = []
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                    summary.append({
                        "timestamp": rec.get("timestamp"),
                        "count":     len(rec.get("vessels", [])),
                    })
                except Exception:
                    pass
        self._json(summary)

    def _api_export_csv(self):
        with _lock:
            path = _state["csv_file"]
        if not path or not os.path.exists(path):
            self.send_response(404)
            self.end_headers()
            return
        with open(path, "rb") as f:
            body = f.read()
        fname = os.path.basename(path)
        self.send_response(200)
        self.send_header("Content-Type", "text/csv; charset=utf-8")
        self.send_header("Content-Disposition", f'attachment; filename="{fname}"')
        self.send_header("Content-Length", len(body))
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    def _api_export_json(self):
        with _lock:
            path = _state["session_file"]
        if not path or not os.path.exists(path):
            self.send_response(404)
            self.end_headers()
            return
        with open(path, "rb") as f:
            body = f.read()
        fname = os.path.basename(path)
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Disposition", f'attachment; filename="{fname}"')
        self.send_header("Content-Length", len(body))
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    def _serve_html(self):
        body = _HTML.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", len(body))
        self.end_headers()
        self.wfile.write(body)


# ── Embedded HTML viewer ───────────────────────────────────────────────────────

_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>AISHub – English Channel</title>
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.min.css"/>
<script src="https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.min.js"></script>
<style>
* { margin:0; padding:0; box-sizing:border-box; }
body { font-family:'SF Mono','Consolas','Menlo',monospace; background:#0a0e14; color:#c4cad4; }
#map { width:100%; height:100vh; }

/* ── control panel ── */
.panel {
  position:absolute; top:12px; right:12px; z-index:1000;
  background:rgba(10,14,20,.93); border:1px solid #1e2a3a;
  border-radius:8px; padding:14px; width:300px;
  backdrop-filter:blur(12px);
}
.panel h2 { font-size:13px; font-weight:600; color:#7eb8da; margin-bottom:12px; letter-spacing:.4px; }

.stat-row { display:flex; justify-content:space-between; font-size:11px; line-height:2; }
.stat-row .label { color:#6b7785; }
.stat-row .val   { color:#c4cad4; font-weight:600; }
.val.ok  { color:#30a050; }
.val.err { color:#e05555; }
.val.warn{ color:#a08030; }

.sep { border:none; border-top:1px solid #1e2a3a; margin:10px 0; }

.btn-row { display:flex; gap:7px; margin-top:4px; }
.btn {
  flex:1; padding:7px 0; border:none; border-radius:4px; cursor:pointer;
  font-family:inherit; font-size:11px; font-weight:600; letter-spacing:.3px;
  transition:all .15s;
}
.btn-rec-start  { background:#1a3d1a; color:#7ed87e; border:1px solid #2a5a2a; }
.btn-rec-start:hover  { background:#1f4d1f; }
.btn-rec-stop   { background:#3d1a1a; color:#e07e7e; border:1px solid #5a2a2a; }
.btn-rec-stop:hover   { background:#4d1f1f; }
.btn-dl-csv     { background:#1a2d3d; color:#7eb8da; border:1px solid #2a4a5a; }
.btn-dl-csv:hover     { background:#1f3a4d; }
.btn-dl-json    { background:#1e1a3d; color:#a07eda; border:1px solid #3a2a5a; }
.btn-dl-json:hover    { background:#271f4d; }

/* ── countdown ── */
#countdown { font-size:24px; font-weight:700; color:#3a6b8a; text-align:center; letter-spacing:1px; padding:6px 0; }
#countdown.imminent { color:#a08030; }

/* ── status dot ── */
.dot {
  display:inline-block; width:7px; height:7px; border-radius:50%;
  margin-right:5px; vertical-align:middle;
}
.dot.off   { background:#4a3030; }
.dot.on    { background:#30a050; }
.dot.pulse { background:#30a050; animation:blink 1s infinite; }
@keyframes blink { 0%,100%{opacity:1} 50%{opacity:.3} }

/* ── legend ── */
.legend {
  margin-top:10px; padding-top:10px; border-top:1px solid #1e2a3a;
  display:grid; grid-template-columns:1fr 1fr; gap:3px 10px; font-size:10px;
}
.legend-item { display:flex; align-items:center; gap:5px; color:#6b7785; }
.swatch { width:10px; height:10px; border-radius:2px; flex-shrink:0; }

/* ── history panel ── */
.hist-panel {
  position:absolute; bottom:12px; left:12px; z-index:1000;
  background:rgba(10,14,20,.93); border:1px solid #1e2a3a;
  border-radius:8px; padding:10px; width:340px; max-height:200px;
  backdrop-filter:blur(12px); overflow:hidden;
}
.hist-panel h3 { font-size:11px; color:#6b7785; margin-bottom:6px; }
#hist-list {
  font-size:10px; max-height:155px; overflow-y:auto;
  color:#5a6570; line-height:1.7;
}
.hist-row { display:flex; justify-content:space-between; border-bottom:1px solid #141a22; padding:1px 0; }
.hist-row .ht { color:#3a4550; }
.hist-row .hv { color:#7eb8da; font-weight:600; }

/* ── error banner ── */
#err-banner {
  display:none; position:absolute; top:12px; left:50%; transform:translateX(-50%);
  z-index:2000; background:#3d1a1a; border:1px solid #8a2020; border-radius:6px;
  padding:8px 16px; font-size:12px; color:#e07e7e; max-width:500px; text-align:center;
}

/* ── leaflet popup override ── */
.leaflet-popup-content-wrapper {
  background:rgba(10,14,20,.95) !important; border:1px solid #1e2a3a !important;
  border-radius:6px !important; box-shadow:0 4px 20px rgba(0,0,0,.6) !important;
}
.leaflet-popup-tip { background:rgba(10,14,20,.95) !important; }
.leaflet-popup-content { margin:10px 14px !important; font-family:'SF Mono','Consolas','Menlo',monospace !important; font-size:11px !important; color:#c4cad4 !important; }
.popup-title { font-size:13px; font-weight:700; color:#7eb8da; margin-bottom:7px; }
.popup-grid { display:grid; grid-template-columns:auto 1fr; gap:2px 10px; font-size:10px; }
.popup-grid .pk { color:#6b7785; }
.popup-grid .pv { color:#c4cad4; font-weight:600; }
</style>
</head>
<body>

<div id="map"></div>
<div id="err-banner"></div>

<!-- control panel -->
<div class="panel">
  <h2>&#9875; AISHub — English Channel</h2>

  <div class="stat-row">
    <span class="label">Status</span>
    <span><span id="dot" class="dot off"></span><span id="status-text" class="val">Waiting…</span></span>
  </div>
  <div class="stat-row">
    <span class="label">Vessels on map</span>
    <span id="vessel-count" class="val">—</span>
  </div>
  <div class="stat-row">
    <span class="label">Last update</span>
    <span id="last-update" class="val">—</span>
  </div>
  <div class="stat-row">
    <span class="label">Polls completed</span>
    <span id="poll-count" class="val">—</span>
  </div>

  <hr class="sep">
  <div style="font-size:10px;color:#6b7785;text-align:center;margin-bottom:4px">next poll in</div>
  <div id="countdown">—</div>

  <hr class="sep">
  <div class="stat-row">
    <span class="label">Recording</span>
    <span id="rec-status" class="val warn">—</span>
  </div>
  <div class="stat-row">
    <span class="label">Records saved</span>
    <span id="rec-count" class="val">—</span>
  </div>
  <div class="stat-row" style="font-size:10px;word-break:break-all;">
    <span class="label" style="flex-shrink:0;margin-right:6px">File</span>
    <span id="rec-file" class="val" style="color:#3a5570;text-align:right">—</span>
  </div>

  <div class="btn-row" style="margin-top:8px">
    <button class="btn btn-rec-start" id="btn-start" onclick="setRecording(true)">&#9679; Record</button>
    <button class="btn btn-rec-stop"  id="btn-stop"  onclick="setRecording(false)">&#9632; Pause</button>
  </div>
  <div class="btn-row" style="margin-top:6px">
    <button class="btn btn-dl-csv"  onclick="download('csv')">&#8659; CSV</button>
    <button class="btn btn-dl-json" onclick="download('json')">&#8659; JSONL</button>
  </div>

  <div class="legend">
    <div class="legend-item"><div class="swatch" style="background:#e05555"></div>Tanker</div>
    <div class="legend-item"><div class="swatch" style="background:#3a9a50"></div>Cargo</div>
    <div class="legend-item"><div class="swatch" style="background:#3a7ae0"></div>Passenger</div>
    <div class="legend-item"><div class="swatch" style="background:#d4a030"></div>Fishing</div>
    <div class="legend-item"><div class="swatch" style="background:#9a50c0"></div>Tug / Pilot</div>
    <div class="legend-item"><div class="swatch" style="background:#d46020"></div>High-speed</div>
    <div class="legend-item"><div class="swatch" style="background:#30a0a0"></div>Sail / Leisure</div>
    <div class="legend-item"><div class="swatch" style="background:#607080"></div>Other</div>
  </div>
</div>

<!-- history panel -->
<div class="hist-panel">
  <h3>Poll history (this session)</h3>
  <div id="hist-list"><span style="color:#3a4550">No polls yet…</span></div>
</div>

<script>
const API = 'http://localhost:8082';

// ── Map setup ────────────────────────────────────────────────────────────────
const map = L.map('map', { center:[50.0, -1.5], zoom:8, zoomControl:true });

L.tileLayer('https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png', {
  attribution:'&copy; <a href="https://www.openstreetmap.org/">OpenStreetMap</a> &copy; <a href="https://carto.com/">CARTO</a>',
  maxZoom:18,
}).addTo(map);

// ── Vessel type → colour ─────────────────────────────────────────────────────
function shipColour(type) {
  const t = Number(type) || 0;
  if (t >= 80 && t <= 89) return '#e05555'; // tanker
  if (t >= 70 && t <= 79) return '#3a9a50'; // cargo
  if (t >= 60 && t <= 69) return '#3a7ae0'; // passenger
  if (t === 30)            return '#d4a030'; // fishing
  if ([21,22,31,32,50,51,52,53,54,55,56,57,58,59].includes(t)) return '#9a50c0';
  if (t >= 40 && t <= 49) return '#d46020'; // HSC
  if (t === 36 || t === 37) return '#30a0a0'; // sail/pleasure
  return '#607080';
}

function shipLabel(type) {
  const t = Number(type) || 0;
  if (t >= 80 && t <= 89) return 'Tanker';
  if (t >= 70 && t <= 79) return 'Cargo';
  if (t >= 60 && t <= 69) return 'Passenger';
  if (t === 30)            return 'Fishing';
  if (t >= 50 && t <= 59) return 'Special';
  if (t === 31 || t === 32) return 'Tug';
  if (t >= 40 && t <= 49) return 'High-speed';
  if (t === 36 || t === 37) return 'Sail/Leisure';
  return `Type ${t}`;
}

// ── Arrow SVG marker ─────────────────────────────────────────────────────────
function arrowIcon(colour, heading) {
  const h = (isNaN(heading) || heading === 511) ? 0 : heading;
  const svg = `<svg xmlns="http://www.w3.org/2000/svg" width="22" height="22" viewBox="-11 -11 22 22">
    <g transform="rotate(${h})">
      <polygon points="0,-10 6,6 0,2 -6,6" fill="${colour}" stroke="rgba(0,0,0,.5)" stroke-width="1"/>
    </g>
  </svg>`;
  return L.divIcon({
    html: svg,
    className:'',
    iconSize:[22,22],
    iconAnchor:[11,11],
  });
}

// ── State ────────────────────────────────────────────────────────────────────
const markers    = {};   // MMSI → L.marker
let   nextPollAt = null;
let   countdownTmr = null;

// ── Fetch & render ────────────────────────────────────────────────────────────
async function fetchVessels() {
  try {
    const res = await fetch(`${API}/api/vessels`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();

    if (data.error) showError(data.error);
    else             hideError();

    updateMap(data.vessels || []);
    if (data.next_poll_at) startCountdown(data.next_poll_at);

    document.getElementById('vessel-count').textContent = (data.vessels||[]).length;
    document.getElementById('last-update').textContent  = data.last_update
      ? new Date(data.last_update).toLocaleTimeString() : '—';
    document.getElementById('poll-count').textContent   = data.poll_count ?? '—';

    const dot  = document.getElementById('dot');
    const stxt = document.getElementById('status-text');
    dot.className  = 'dot on';
    stxt.textContent = 'Live';
    stxt.className   = 'val ok';
  } catch(e) {
    showError('Cannot reach tracker — is aishub_tracker.py running?');
    const dot  = document.getElementById('dot');
    const stxt = document.getElementById('status-text');
    dot.className    = 'dot off';
    stxt.textContent = 'Offline';
    stxt.className   = 'val err';
  }
}

async function fetchStatus() {
  try {
    const res  = await fetch(`${API}/api/status`);
    const data = await res.json();

    const recEl   = document.getElementById('rec-status');
    const recCnt  = document.getElementById('rec-count');
    const recFile = document.getElementById('rec-file');

    if (data.recording) {
      recEl.textContent = '● Recording';
      recEl.className   = 'val ok';
    } else {
      recEl.textContent = '◌ Paused';
      recEl.className   = 'val warn';
    }
    recCnt.textContent  = data.total_records ?? '—';
    const f = data.session_file || '';
    recFile.textContent = f ? f.replace(/.*\//, '') : '—';
  } catch(_) {}
}

async function fetchHistory() {
  try {
    const res  = await fetch(`${API}/api/history`);
    const rows = await res.json();
    const el   = document.getElementById('hist-list');
    if (!rows.length) { el.innerHTML = '<span style="color:#3a4550">No polls yet…</span>'; return; }
    el.innerHTML = rows.slice().reverse().map(r => {
      const t = r.timestamp ? new Date(r.timestamp).toLocaleTimeString() : '?';
      return `<div class="hist-row"><span class="ht">${t}</span><span class="hv">${r.count} vessels</span></div>`;
    }).join('');
  } catch(_) {}
}

// ── Map update ────────────────────────────────────────────────────────────────
function updateMap(vessels) {
  const seen = new Set();

  vessels.forEach(v => {
    const mmsi = String(v.MMSI);
    const lat  = parseFloat(v.LATITUDE);
    const lon  = parseFloat(v.LONGITUDE);
    if (isNaN(lat) || isNaN(lon)) return;

    seen.add(mmsi);
    const col  = shipColour(v.TYPE);
    const hdg  = parseFloat(v.HEADING);
    const icon = arrowIcon(col, hdg);

    if (markers[mmsi]) {
      markers[mmsi].setLatLng([lat, lon]);
      markers[mmsi].setIcon(icon);
    } else {
      const m = L.marker([lat, lon], { icon }).addTo(map);
      m.bindPopup(() => buildPopup(v), { maxWidth:280 });
      markers[mmsi] = m;
    }
    // keep popup data fresh
    markers[mmsi]._aisData = v;
    markers[mmsi].on('click', function() {
      this.getPopup().setContent(buildPopup(this._aisData));
    });
  });

  // remove stale markers
  Object.keys(markers).forEach(mmsi => {
    if (!seen.has(mmsi)) {
      map.removeLayer(markers[mmsi]);
      delete markers[mmsi];
    }
  });
}

function buildPopup(v) {
  const name   = v.NAME    ? v.NAME.trim()    : 'Unknown';
  const dest   = v.DEST    ? v.DEST.trim()    : '—';
  const navstLabels = ['Under way (engine)','At anchor','Not under command','Restricted manoeuvre',
    'Constrained by draught','Moored','Aground','Engaged in fishing','Under way (sailing)'];
  const ns = navstLabels[v.NAVSTAT] || `${v.NAVSTAT}`;
  const w  = (n,u='')  => (n!==undefined && n!==null && n!=='' && n!==511) ? `${n}${u}` : '—';

  return `<div class="popup-title">${name}</div>
<div class="popup-grid">
  <span class="pk">MMSI</span>     <span class="pv">${v.MMSI}</span>
  <span class="pk">IMO</span>      <span class="pv">${w(v.IMO)}</span>
  <span class="pk">Call sign</span><span class="pv">${w(v.CALLSIGN)}</span>
  <span class="pk">Type</span>     <span class="pv">${shipLabel(v.TYPE)} (${v.TYPE})</span>
  <span class="pk">Nav status</span><span class="pv">${ns}</span>
  <span class="pk">Speed</span>    <span class="pv">${w(v.SOG,' kn')}</span>
  <span class="pk">Course</span>   <span class="pv">${w(v.COG,'°')}</span>
  <span class="pk">Heading</span>  <span class="pv">${(v.HEADING!==511)?w(v.HEADING,'°'):'—'}</span>
  <span class="pk">Draught</span>  <span class="pv">${w(v.DRAUGHT,' m')}</span>
  <span class="pk">Destination</span><span class="pv">${dest}</span>
  <span class="pk">ETA</span>      <span class="pv">${w(v.ETA)}</span>
  <span class="pk">Lat / Lon</span><span class="pv">${parseFloat(v.LATITUDE).toFixed(4)}° / ${parseFloat(v.LONGITUDE).toFixed(4)}°</span>
  <span class="pk">Last seen</span><span class="pv">${v.TIME||'—'}</span>
</div>`;
}

// ── Countdown ─────────────────────────────────────────────────────────────────
function startCountdown(isoString) {
  nextPollAt = new Date(isoString).getTime();
  if (countdownTmr) clearInterval(countdownTmr);
  countdownTmr = setInterval(() => {
    const secs = Math.max(0, Math.round((nextPollAt - Date.now()) / 1000));
    const el   = document.getElementById('countdown');
    el.textContent = `${secs}s`;
    el.className   = secs <= 10 ? 'imminent' : '';
    if (secs === 0) { clearInterval(countdownTmr); countdownTmr = null; }
  }, 500);
}

// ── Error banner ──────────────────────────────────────────────────────────────
function showError(msg) {
  const el = document.getElementById('err-banner');
  el.textContent = '⚠ ' + msg;
  el.style.display = 'block';
}
function hideError() {
  document.getElementById('err-banner').style.display = 'none';
}

// ── Recording controls ────────────────────────────────────────────────────────
async function setRecording(on) {
  await fetch(`${API}/api/recording/${on ? 'start' : 'stop'}`, { method:'POST' });
  fetchStatus();
}

function download(type) {
  window.location.href = `${API}/api/export/${type}`;
}

// ── Poll loop ─────────────────────────────────────────────────────────────────
async function refresh() {
  await Promise.all([fetchVessels(), fetchStatus(), fetchHistory()]);
}

refresh();
setInterval(refresh, 60_000);  // re-fetch every 60 s (matches server poll interval)
setInterval(fetchStatus,  5_000); // status bar updates more often
setInterval(fetchHistory, 15_000);
</script>
</body>
</html>
"""

# ── Entry point ────────────────────────────────────────────────────────────────

def main():
    print("─" * 52)
    print("  AISHub English Channel Tracker")
    print("─" * 52)
    print(f"  API key   : {API_KEY}")
    print(f"  Bounding  : {LAT_MIN}°N–{LAT_MAX}°N  {LON_MIN}°E–{LON_MAX}°E")
    print(f"  Interval  : {POLL_INTERVAL} s  (API rate limit)")
    print(f"  Data dir  : {os.path.abspath(DATA_DIR)}/")
    print(f"  Database  : {DB_PATH}")
    print(f"  Viewer    : http://localhost:{PORT}")
    print("─" * 52)
    print()

    # Start background poller
    t = threading.Thread(target=_poll_loop, daemon=True, name="poller")
    t.start()

    # Start HTTP server
    server = _Server(("localhost", PORT), _Handler)
    print(f"Open http://localhost:{PORT} in your browser.")
    print("Press Ctrl+C to stop.\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[stopped]")
    finally:
        STORE.close()


if __name__ == "__main__":
    main()
