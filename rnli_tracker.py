#!/usr/bin/env python3
"""
RNLI Lifeboat Tracker
Polls AISHub REST API (max once/minute) for the full UK & Irish coastline,
filters results to the 162 known RNLI MMSIs, and displays them on an
interactive map.  Data is continuously recorded to rnli_data/.

Usage:
  python3 rnli_tracker.py

Then open http://localhost:8083 in your browser.
"""

import json
import time
import threading
import csv
import gzip
import os
import sys
import fcntl
import urllib.request
import urllib.parse
from datetime import datetime, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler

# ── Configuration ──────────────────────────────────────────────────────────────
API_KEY        = "AH_2670_9F0D1564"
API_URL        = "https://data.aishub.net/ws.php"
POLL_INTERVAL  = 60          # seconds — DO NOT reduce below 60
PORT           = 8083
DATA_DIR       = "rnli_data"
# Shared with aishub_tracker.py — one file governs the rate limit for the whole API key
LAST_POLL_FILE = os.path.expanduser("~/.aishub_last_poll")

# UK + Irish coastline bounding box  (catches all RNLI stations)
LAT_MIN, LAT_MAX =  49.0, 61.5
LON_MIN, LON_MAX = -11.0,  3.0

# ── Border Force vessels (subset of the MMSI list — shown in a different colour) ──
BORDER_FORCE_MMSIS = {
    '235521000', '236112138', '236112137', '235745000',
    '235101211', '235118128', '235118129', '235118131', '235118132',
}

# ── 162 RNLI MMSIs extracted from RNLI_MMSI_List.xlsx ─────────────────────────
RNLI_MMSIS = {
    '232046511', '232059250', '232009305', '232012724', '232019619', '235109054', '232040641', '235095307',
    '232023288', '232046186', '232042680', '235005119', '232053077', '232046187', '232054391', '232027360',
    '235109052', '232026434', '232026437', '235095911', '235113728', '232002440', '232046513', '235109055',
    '232024687', '232027356', '235088862', '235030385', '232002340', '235101098', '232053081', '232001910',
    '232052719', '232024688', '235080605', '232025941', '235080606', '235080607', '235070739', '232009307',
    '235069215', '232053082', '232002480', '235101095', '232009175', '232001840', '232003141', '235050721',
    '235109063', '235050723', '232004401', '235102921', '235106583', '235005118', '232004396', '235050568',
    '232001940', '235101096', '232004407', '235050655', '235005121', '232059197', '235069212', '235005122',
    '232003049', '235050719', '235106579', '235030388', '235050722', '235092934', '235106576', '232051614',
    '235069218', '235014279', '235050567', '235069182', '232001860', '232027359', '232009189', '232046507',
    '232025975', '235069216', '232002182', '235069217', '235109056', '232009301', '232026001', '232002380',
    '232009302', '235106573', '232024689', '235113732', '232032292', '235109051', '232024686', '235050564',
    '235109062', '235098755', '235092932', '235113733', '235113731', '232059196', '232003139', '232027361',
    '235007798', '232002470', '232004402', '232003050', '232002390', '235007797', '232003136', '235007795',
    '235030387', '232003051', '232003134', '232002490', '232003052', '232002183', '232009306', '235007809',
    '235005113', '235030389', '232004399', '235106578', '232004404', '232027358', '235087721', '235005114',
    '232009187', '232004409', '232004405', '232002582', '235090694', '235003642', '232001880', '232009169',
    '232025390', '235010875', '235050725', '232025976', '232003131', '232027355', '235069214', '235010876',
    '232002460', '232003137', '232002450', '232026003', '232003138', '232003133', '232003142', '235106575',
    '235089703', '235521000', '236112138', '236112137', '235745000', '235101211', '235118128', '235118129',
    '235118131', '235118132',
}

# ── Shared state ───────────────────────────────────────────────────────────────
_state = {
    "vessels":       [],
    "last_update":   None,
    "next_poll_at":  None,
    "poll_count":    0,
    "error":         None,
    "recording":     True,
    "total_records": 0,
    "session_file":  None,
    "csv_file":      None,
}
_lock = threading.Lock()

# ── Polling ────────────────────────────────────────────────────────────────────

def _fetch_raw():
    params = urllib.parse.urlencode({
        "username": API_KEY,
        "format":   1,
        "output":   "json",
        "compress": 0,
        "latmin":   LAT_MIN,
        "latmax":   LAT_MAX,
        "lonmin":   LON_MIN,
        "lonmax":   LON_MAX,
    })
    req = urllib.request.Request(
        f"{API_URL}?{params}", headers={"User-Agent": "RNLITracker/1.0"}
    )
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
    call time.  Uses an exclusive file lock so that aishub_tracker.py and
    rnli_tracker.py running concurrently cannot both fire within the same
    60-second window — they share ~/.aishub_last_poll.
    """
    while True:
        remaining = float(POLL_INTERVAL)
        try:
            with open(LAST_POLL_FILE, "a+") as fh:
                fcntl.flock(fh, fcntl.LOCK_EX)      # blocks if the other tracker holds it
                fh.seek(0)
                content = fh.read().strip()
                last_ts = float(json.loads(content).get("ts", 0)) if content else 0
                elapsed = time.time() - last_ts

                if elapsed >= POLL_INTERVAL:
                    fh.seek(0); fh.truncate()
                    json.dump({"ts": time.time()}, fh)
                    fh.flush()
                    return          # context manager releases the lock
                remaining = max(1.0, POLL_INTERVAL - elapsed)
        except Exception:
            pass

        with _lock:
            _state["next_poll_at"] = datetime.fromtimestamp(
                time.time() + remaining, tz=timezone.utc
            ).isoformat()
        time.sleep(min(remaining, 5))


def _open_session_files():
    _ensure_data_dir()
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    jsonl = os.path.join(DATA_DIR, f"rnli_{stamp}.jsonl")
    csv_f = os.path.join(DATA_DIR, f"rnli_{stamp}.csv")
    with open(csv_f, "w", newline="") as f:
        csv.writer(f).writerow([
            "poll_timestamp", "MMSI", "NAME", "CALLSIGN", "TYPE",
            "LATITUDE", "LONGITUDE", "SOG", "COG", "HEADING",
            "NAVSTAT", "ROT", "IMO", "DRAUGHT", "DEST", "ETA",
            "A", "B", "C", "D",
        ])
    return jsonl, csv_f


def _append_record(timestamp, vessels, meta, jsonl_path, csv_path):
    with open(jsonl_path, "a", encoding="utf-8") as f:
        f.write(json.dumps({"timestamp": timestamp, "meta": meta,
                             "vessels": vessels}) + "\n")
    with open(csv_path, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        for v in vessels:
            w.writerow([
                timestamp,
                v.get("MMSI",""),    v.get("NAME",""),
                v.get("CALLSIGN",""),v.get("TYPE",""),
                v.get("LATITUDE",""),v.get("LONGITUDE",""),
                v.get("SOG",""),     v.get("COG",""),
                v.get("HEADING",""), v.get("NAVSTAT",""),
                v.get("ROT",""),     v.get("IMO",""),
                v.get("DRAUGHT",""), v.get("DEST",""),
                v.get("ETA",""),     v.get("A",""),
                v.get("B",""),       v.get("C",""),
                v.get("D",""),
            ])


def _poll_loop():
    jsonl_path, csv_path = _open_session_files()
    with _lock:
        _state["session_file"] = jsonl_path
        _state["csv_file"]     = csv_path

    _ts = lambda: datetime.now().strftime("%H:%M:%S")

    while True:
        _wait_for_poll_slot()   # blocks until safe; handles cooldown + concurrent scripts
        poll_start = time.monotonic()
        try:
            print(f"[{_ts()}] Polling AISHub API (UK coast)…")
            data = _fetch_raw()

            meta        = data[0] if isinstance(data[0], dict) else {}
            all_vessels = data[1] if len(data) > 1 else []

            if meta.get("ERROR"):
                raise RuntimeError(f"API error: {meta}")

            # Filter to RNLI fleet only
            rnli = [v for v in all_vessels
                    if str(v.get("MMSI", "")) in RNLI_MMSIS]

            timestamp = datetime.now(timezone.utc).isoformat()
            next_at   = datetime.fromtimestamp(
                time.time() + POLL_INTERVAL, tz=timezone.utc
            ).isoformat()

            with _lock:
                _state["vessels"]      = rnli
                _state["last_update"]  = timestamp
                _state["next_poll_at"] = next_at
                _state["poll_count"]  += 1
                _state["error"]        = None

                if _state["recording"] and rnli:
                    _append_record(timestamp, rnli, meta, jsonl_path, csv_path)
                    _state["total_records"] += len(rnli)

            print(f"  ↳ {len(rnli)} RNLI vessels visible  "
                  f"(of {len(all_vessels)} in area)  |  poll #{_state['poll_count']}")

        except Exception as exc:
            err = f"{type(exc).__name__}: {exc}"
            with _lock:
                _state["error"] = err
            print(f"  [ERROR] {err}", file=sys.stderr)

        elapsed = time.monotonic() - poll_start
        time.sleep(max(0, POLL_INTERVAL - elapsed))


# ── HTTP server ────────────────────────────────────────────────────────────────

class _Server(HTTPServer):
    allow_reuse_address = True


class _Handler(BaseHTTPRequestHandler):

    def do_OPTIONS(self):
        self.send_response(200); self._cors(); self.end_headers()

    def do_GET(self):
        path = self.path.split("?")[0].rstrip("/")
        if   path in ("", "/"):            self._serve_html()
        elif path == "/api/status":        self._api_status()
        elif path == "/api/vessels":       self._api_vessels()
        elif path == "/api/history":       self._api_history()
        elif path == "/api/export/csv":    self._api_export("csv")
        elif path == "/api/export/json":   self._api_export("jsonl")
        else:
            self.send_response(404); self.end_headers()

    def do_POST(self):
        path = self.path.split("?")[0].rstrip("/")
        if   path == "/api/recording/start":
            with _lock: _state["recording"] = True
            self._json({"recording": True})
        elif path == "/api/recording/stop":
            with _lock: _state["recording"] = False
            self._json({"recording": False})
        else:
            self.send_response(404); self.end_headers()

    def _json(self, obj):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", len(body))
        self._cors(); self.end_headers(); self.wfile.write(body)

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin",  "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def log_message(self, *_): pass

    def _api_status(self):
        with _lock:
            snap = {k: v for k, v in _state.items() if k != "vessels"}
            snap["vessel_count"] = len(_state["vessels"])
        snap["poll_interval"] = POLL_INTERVAL
        snap["fleet_size"]    = len(RNLI_MMSIS)
        self._json(snap)

    def _api_vessels(self):
        with _lock:
            self._json({
                "vessels":      _state["vessels"],
                "last_update":  _state["last_update"],
                "next_poll_at": _state["next_poll_at"],
                "poll_count":   _state["poll_count"],
                "error":        _state["error"],
            })

    def _api_history(self):
        with _lock: path = _state["session_file"]
        if not path or not os.path.exists(path):
            self._json([]); return
        summary = []
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line: continue
                try:
                    r = json.loads(line)
                    summary.append({"timestamp": r.get("timestamp"),
                                    "count": len(r.get("vessels", []))})
                except Exception: pass
        self._json(summary)

    def _api_export(self, fmt):
        with _lock:
            path = _state["csv_file"] if fmt == "csv" else _state["session_file"]
        if not path or not os.path.exists(path):
            self.send_response(404); self.end_headers(); return
        with open(path, "rb") as f: body = f.read()
        ct = "text/csv" if fmt == "csv" else "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", ct)
        self.send_header("Content-Disposition",
                         f'attachment; filename="{os.path.basename(path)}"')
        self.send_header("Content-Length", len(body))
        self._cors(); self.end_headers(); self.wfile.write(body)

    def _serve_html(self):
        # Inject the Border Force MMSI set so the browser can colour them differently
        bf_js  = "const BORDER_FORCE_MMSIS = new Set(%s);\n" % json.dumps(
            sorted(BORDER_FORCE_MMSIS))
        html   = _HTML.replace("/*INJECT_BF_MMSIS*/", bf_js)
        body   = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", len(body))
        self.end_headers(); self.wfile.write(body)


# ── Embedded HTML viewer ───────────────────────────────────────────────────────

_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>RNLI Lifeboat Tracker</title>
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.min.css"/>
<script src="https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.min.js"></script>
<style>
* { margin:0; padding:0; box-sizing:border-box; }
body { font-family:'SF Mono','Consolas','Menlo',monospace; background:#0a0d12; color:#d4cfc8; }
#map { width:100%; height:100vh; }

.panel {
  position:absolute; top:12px; right:12px; z-index:1000;
  background:rgba(8,11,17,.94); border:1px solid #2a1f10;
  border-radius:8px; padding:14px; width:300px;
  backdrop-filter:blur(12px);
}
.panel-header {
  display:flex; align-items:center; gap:10px; margin-bottom:12px;
}
.rnli-logo {
  width:36px; height:36px; border-radius:4px; flex-shrink:0;
  background:#F57500; display:flex; align-items:center; justify-content:center;
  font-size:18px; font-weight:900; color:#fff; letter-spacing:-1px;
}
.panel-header h2 { font-size:13px; font-weight:700; color:#F5A050; letter-spacing:.3px; }
.panel-header .sub { font-size:10px; color:#7a6a55; margin-top:1px; }

.stat-row { display:flex; justify-content:space-between; font-size:10px; line-height:2; }
.stat-row .lbl { color:#6b6055; }
.stat-row .val { color:#d4cfc8; font-weight:600; }
.val.ok  { color:#F57500; }
.val.err { color:#e05555; }
.val.warn{ color:#a08030; }
.sep { border:none; border-top:1px solid #2a1f10; margin:8px 0; }

.btn-row { display:flex; gap:7px; margin-top:4px; }
.btn {
  flex:1; padding:7px 0; border:none; border-radius:4px; cursor:pointer;
  font-family:inherit; font-size:11px; font-weight:700; letter-spacing:.3px;
  transition:all .15s;
}
.btn-rec-start { background:#3d2000; color:#F5A050; border:1px solid #6a3800; }
.btn-rec-start:hover { background:#4d2800; }
.btn-rec-stop  { background:#3d1a1a; color:#e07e7e; border:1px solid #5a2a2a; }
.btn-rec-stop:hover  { background:#4d1f1f; }
.btn-dl        { background:#1a1d2a; color:#8090b8; border:1px solid #2a3040; }
.btn-dl:hover  { background:#222535; }

#countdown { font-size:24px; font-weight:700; color:#4a3010; text-align:center;
             letter-spacing:1px; padding:6px 0; }
#countdown.imminent { color:#F57500; }

.dot { display:inline-block; width:7px; height:7px; border-radius:50%;
       margin-right:5px; vertical-align:middle; }
.dot.off  { background:#4a3030; }
.dot.on   { background:#F57500; animation:glow 2s infinite; }
@keyframes glow { 0%,100%{box-shadow:0 0 3px #F57500} 50%{box-shadow:0 0 8px #F57500} }

/* fleet presence bar */
.presence-bar { margin-top:6px; height:6px; border-radius:3px;
                background:#1a1510; overflow:hidden; }
.presence-fill { height:100%; border-radius:3px; background:#F57500;
                 transition:width .5s; }

.hist-panel {
  position:absolute; bottom:12px; left:12px; z-index:1000;
  background:rgba(8,11,17,.94); border:1px solid #2a1f10;
  border-radius:8px; padding:10px; width:320px; max-height:200px;
  backdrop-filter:blur(12px); overflow:hidden;
}
.hist-panel h3 { font-size:11px; color:#6b6055; margin-bottom:6px; }
#hist-list { font-size:10px; max-height:155px; overflow-y:auto; color:#5a5045; line-height:1.7; }
.hist-row { display:flex; justify-content:space-between; border-bottom:1px solid #141018; padding:1px 0; }
.hist-row .ht { color:#3a3025; }
.hist-row .hv { color:#F57500; font-weight:600; }

#err-banner {
  display:none; position:absolute; top:12px; left:50%; transform:translateX(-50%);
  z-index:2000; background:#3d1a1a; border:1px solid #8a2020;
  border-radius:6px; padding:8px 16px; font-size:12px; color:#e07e7e;
  max-width:500px; text-align:center;
}

.leaflet-popup-content-wrapper {
  background:rgba(8,11,17,.97)!important; border:1px solid #2a1f10!important;
  border-radius:6px!important; box-shadow:0 4px 20px rgba(0,0,0,.7)!important;
}
.leaflet-popup-tip { background:rgba(8,11,17,.97)!important; }
.leaflet-popup-content {
  margin:10px 13px!important;
  font-family:'SF Mono','Consolas','Menlo',monospace!important;
  font-size:11px!important; color:#d4cfc8!important;
}
.pt { font-size:13px; font-weight:700; color:#F5A050; margin-bottom:4px; }
.pt-mmsi { font-size:10px; color:#6b6055; margin-bottom:7px; }
.pg { display:grid; grid-template-columns:auto 1fr; gap:2px 10px; font-size:10px; }
.pk { color:#6b6055; } .pv { color:#d4cfc8; font-weight:600; }
</style>
</head>
<body>
<div id="map"></div>
<div id="err-banner"></div>

<div class="panel">
  <div class="panel-header">
    <div class="rnli-logo">RNLI</div>
    <div>
      <h2>Lifeboat Tracker</h2>
      <div class="sub">Live AIS · UK &amp; Irish Coast</div>
    </div>
  </div>

  <div class="stat-row">
    <span class="lbl">Status</span>
    <span><span id="dot" class="dot off"></span><span id="status-text" class="val">Waiting…</span></span>
  </div>
  <div class="stat-row">
    <span class="lbl">Lifeboats visible</span>
    <span id="vessel-count" class="val">—</span>
  </div>
  <div class="stat-row">
    <span class="lbl">Fleet tracked</span>
    <span id="fleet-pct" class="val">— of 162</span>
  </div>
  <div class="presence-bar"><div class="presence-fill" id="presence-fill" style="width:0%"></div></div>

  <div class="stat-row" style="margin-top:6px">
    <span class="lbl">Last update</span>
    <span id="last-update" class="val">—</span>
  </div>
  <div class="stat-row">
    <span class="lbl">Polls completed</span>
    <span id="poll-count" class="val">—</span>
  </div>

  <hr class="sep">
  <div style="font-size:10px;color:#6b6055;text-align:center;margin-bottom:4px">next poll in</div>
  <div id="countdown">—</div>

  <hr class="sep">
  <div class="stat-row">
    <span class="lbl">Recording</span>
    <span id="rec-status" class="val warn">—</span>
  </div>
  <div class="stat-row">
    <span class="lbl">Positions saved</span>
    <span id="rec-count" class="val">—</span>
  </div>

  <div class="btn-row" style="margin-top:8px">
    <button class="btn btn-rec-start" onclick="setRecording(true)">&#9679; Record</button>
    <button class="btn btn-rec-stop"  onclick="setRecording(false)">&#9632; Pause</button>
  </div>
  <div class="btn-row" style="margin-top:6px">
    <button class="btn btn-dl" onclick="download('csv')">&#8659; CSV</button>
    <button class="btn btn-dl" onclick="download('json')">&#8659; JSONL</button>
  </div>

  <hr class="sep">
  <div style="display:flex;gap:14px;font-size:10px;color:#6b6055">
    <div style="display:flex;align-items:center;gap:6px">
      <div style="width:12px;height:12px;border-radius:2px;background:#F57500;flex-shrink:0"></div>
      RNLI lifeboat
    </div>
    <div style="display:flex;align-items:center;gap:6px">
      <div style="width:12px;height:12px;border-radius:2px;background:#1e7fd4;flex-shrink:0"></div>
      Border Force
    </div>
  </div>
</div>

<div class="hist-panel">
  <h3>Poll history — lifeboats visible per poll</h3>
  <div id="hist-list"><span style="color:#3a3025">No polls yet…</span></div>
</div>

<script>
/*INJECT_BF_MMSIS*/
const API = 'http://localhost:8083';
const FLEET_SIZE = 162;

const COLOUR_RNLI  = '#F57500';   // RNLI orange
const COLOUR_BF    = '#1e7fd4';   // Border Force blue

// ── Map ────────────────────────────────────────────────────────────────────
const map = L.map('map', { center:[55.5, -4.5], zoom:6 });
L.tileLayer('https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png', {
  attribution:'&copy; OpenStreetMap &copy; CARTO', maxZoom:18,
}).addTo(map);

// Arrow icon — colour determined by MMSI
const iconCache = {};
function vesselIcon(mmsi, heading) {
  const colour = BORDER_FORCE_MMSIS.has(String(mmsi)) ? COLOUR_BF : COLOUR_RNLI;
  const h = (isNaN(heading) || heading === 511 || heading === null)
    ? 0 : Math.round(heading / 5) * 5;
  const key = colour + '_' + h;
  if (iconCache[key]) return iconCache[key];
  const svg = `<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="-12 -12 24 24">
    <g transform="rotate(${h})">
      <polygon points="0,-11 7,7 0,3 -7,7" fill="${colour}" stroke="rgba(0,0,0,.6)" stroke-width="1.5"/>
    </g>
  </svg>`;
  const icon = L.divIcon({ html:svg, className:'', iconSize:[24,24], iconAnchor:[12,12] });
  iconCache[key] = icon;
  return icon;
}

const markers = {};
const NAVSTAT = ['Under way (engine)','At anchor','Not under command',
  'Restricted manoeuvrability','Constrained by draught','Moored','Aground',
  'Engaged in fishing','Under way (sailing)'];

function updateMap(vessels) {
  const seen = new Set();
  vessels.forEach(v => {
    const mmsi = String(v.MMSI);
    const lat  = parseFloat(v.LATITUDE);
    const lon  = parseFloat(v.LONGITUDE);
    if (isNaN(lat) || isNaN(lon)) return;
    seen.add(mmsi);
    const icon = vesselIcon(mmsi, parseFloat(v.HEADING));
    if (markers[mmsi]) {
      markers[mmsi].setLatLng([lat, lon]);
      markers[mmsi].setIcon(icon);
    } else {
      const m = L.marker([lat, lon], { icon }).addTo(map);
      markers[mmsi] = m;
    }
    markers[mmsi]._data = v;
    markers[mmsi].off('click');
    markers[mmsi].on('click', function() {
      map.openPopup(buildPopup(this._data), [lat, lon], { maxWidth: 290 });
    });
  });
  Object.keys(markers).forEach(mmsi => {
    if (!seen.has(mmsi)) { map.removeLayer(markers[mmsi]); delete markers[mmsi]; }
  });
}

function buildPopup(v) {
  const mmsi     = String(v.MMSI);
  const isBF     = BORDER_FORCE_MMSIS.has(mmsi);
  const titleCol = isBF ? COLOUR_BF : COLOUR_RNLI;
  const org      = isBF ? 'Border Force' : 'RNLI Lifeboat';
  const name     = v.NAME ? v.NAME.trim() : 'Unknown';
  const dest     = v.DEST ? v.DEST.trim() : '—';
  const ns       = NAVSTAT[v.NAVSTAT] || '—';
  const w = (x, u='') => (x !== undefined && x !== null && x !== '' && x !== 511)
    ? `${x}${u}` : '—';
  const pop = L.popup({ maxWidth:290 });
  pop.setContent(`
    <div class="pt" style="color:${titleCol}">${name}</div>
    <div class="pt-mmsi">${org} · MMSI ${mmsi}${v.CALLSIGN?' · '+v.CALLSIGN:''}</div>
    <div class="pg">
      <span class="pk">Status</span>      <span class="pv">${ns}</span>
      <span class="pk">Speed</span>       <span class="pv">${w(v.SOG,' kn')}</span>
      <span class="pk">Course</span>      <span class="pv">${w(v.COG,'°')}</span>
      <span class="pk">Heading</span>     <span class="pv">${v.HEADING!==511?w(v.HEADING,'°'):'—'}</span>
      <span class="pk">Destination</span> <span class="pv">${dest}</span>
      <span class="pk">ETA</span>         <span class="pv">${w(v.ETA)}</span>
      <span class="pk">Draught</span>     <span class="pv">${w(v.DRAUGHT,' m')}</span>
      <span class="pk">Position</span>    <span class="pv">${parseFloat(v.LATITUDE).toFixed(4)}° / ${parseFloat(v.LONGITUDE).toFixed(4)}°</span>
      <span class="pk">Last AIS</span>    <span class="pv">${v.TIME||'—'}</span>
    </div>`);
  return pop;
}

// ── Countdown ──────────────────────────────────────────────────────────────
let nextPollAt = null, cTmr = null;
function startCountdown(iso) {
  nextPollAt = new Date(iso).getTime();
  if (cTmr) clearInterval(cTmr);
  cTmr = setInterval(() => {
    const s = Math.max(0, Math.round((nextPollAt - Date.now()) / 1000));
    const el = document.getElementById('countdown');
    el.textContent = `${s}s`;
    el.className = s <= 10 ? 'imminent' : '';
    if (s === 0) { clearInterval(cTmr); cTmr = null; }
  }, 500);
}

// ── Fetch ──────────────────────────────────────────────────────────────────
async function fetchVessels() {
  try {
    const r = await fetch(`${API}/api/vessels`);
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    const d = await r.json();
    if (d.error) showError(d.error); else hideError();
    const vessels = d.vessels || [];
    updateMap(vessels);
    if (d.next_poll_at) startCountdown(d.next_poll_at);

    const count = vessels.length;
    const pct   = Math.round(count / FLEET_SIZE * 100);
    document.getElementById('vessel-count').textContent = count;
    document.getElementById('fleet-pct').textContent    = `${count} of ${FLEET_SIZE} (${pct}%)`;
    document.getElementById('presence-fill').style.width = pct + '%';
    document.getElementById('last-update').textContent  =
      d.last_update ? new Date(d.last_update).toLocaleTimeString() : '—';
    document.getElementById('poll-count').textContent   = d.poll_count ?? '—';

    const dot = document.getElementById('dot');
    const st  = document.getElementById('status-text');
    dot.className    = 'dot on';
    st.textContent   = 'Live';
    st.className     = 'val ok';
  } catch(e) {
    showError('Cannot reach tracker — is rnli_tracker.py running?');
    document.getElementById('dot').className = 'dot off';
    const st = document.getElementById('status-text');
    st.textContent = 'Offline'; st.className = 'val err';
  }
}

async function fetchStatus() {
  try {
    const d = await (await fetch(`${API}/api/status`)).json();
    const el = document.getElementById('rec-status');
    el.textContent = d.recording ? '● Recording' : '◌ Paused';
    el.className   = d.recording ? 'val ok' : 'val warn';
    document.getElementById('rec-count').textContent = d.total_records ?? '—';
  } catch(_) {}
}

async function fetchHistory() {
  try {
    const rows = await (await fetch(`${API}/api/history`)).json();
    const el = document.getElementById('hist-list');
    if (!rows.length) { el.innerHTML = '<span style="color:#3a3025">No polls yet…</span>'; return; }
    el.innerHTML = rows.slice().reverse().map(r => {
      const t = r.timestamp ? new Date(r.timestamp).toLocaleTimeString() : '?';
      const pct = Math.round(r.count / FLEET_SIZE * 100);
      return `<div class="hist-row"><span class="ht">${t}</span><span class="hv">${r.count} lifeboats (${pct}%)</span></div>`;
    }).join('');
  } catch(_) {}
}

function showError(msg) {
  const el = document.getElementById('err-banner');
  el.textContent = '⚠ ' + msg; el.style.display = 'block';
}
function hideError() { document.getElementById('err-banner').style.display = 'none'; }

async function setRecording(on) {
  await fetch(`${API}/api/recording/${on ? 'start' : 'stop'}`, { method:'POST' });
  fetchStatus();
}
function download(t) { window.location.href = `${API}/api/export/${t}`; }

async function refresh() {
  await Promise.all([fetchVessels(), fetchStatus(), fetchHistory()]);
}

refresh();
setInterval(refresh,      60_000);
setInterval(fetchStatus,   5_000);
setInterval(fetchHistory, 15_000);
</script>
</body>
</html>
"""

# ── Entry point ────────────────────────────────────────────────────────────────

def main():
    print("─" * 52)
    print("  RNLI Lifeboat Tracker")
    print("─" * 52)
    print(f"  API key   : {API_KEY}")
    print(f"  Bounding  : {LAT_MIN}°N–{LAT_MAX}°N  {LON_MIN}°E–{LON_MAX}°E (UK coast)")
    print(f"  Fleet     : {len(RNLI_MMSIS)} RNLI MMSIs")
    print(f"  Interval  : {POLL_INTERVAL} s  (API rate limit)")
    print(f"  Data dir  : {os.path.abspath(DATA_DIR)}/")
    print(f"  Viewer    : http://localhost:{PORT}")
    print("─" * 52)
    print()

    t = threading.Thread(target=_poll_loop, daemon=True, name="poller")
    t.start()

    server = _Server(("localhost", PORT), _Handler)
    print(f"Open http://localhost:{PORT} in your browser.")
    print("Press Ctrl+C to stop.\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[stopped]")


if __name__ == "__main__":
    main()
