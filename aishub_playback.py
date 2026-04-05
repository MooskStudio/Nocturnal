#!/usr/bin/env python3
"""
AISHub Playback Visualiser
Reads a recorded .jsonl (or .csv) session file produced by aishub_tracker.py
and generates a fully standalone animated HTML file — just open it in any browser.

Usage:
  python3 aishub_playback.py                       # auto-picks latest file in ais_data/
  python3 aishub_playback.py path/to/file.jsonl
  python3 aishub_playback.py path/to/file.csv
  python3 aishub_playback.py --list                # list available recordings
"""

import json
import sys
import os
import csv
import glob
import webbrowser
from datetime import datetime, timezone

DATA_DIR = "ais_data"


# ── Data loading ───────────────────────────────────────────────────────────────

def load_jsonl(path: str) -> list[dict]:
    """Load a JSONL recording. Returns list of {timestamp, vessels:[...]} dicts."""
    snapshots = []
    with open(path, encoding="utf-8") as f:
        for lineno, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
                snapshots.append(rec)
            except json.JSONDecodeError as e:
                print(f"  Warning: skipping malformed line {lineno}: {e}")
    return snapshots


def load_csv(path: str) -> list[dict]:
    """Load a CSV recording and reconstruct poll snapshots."""
    rows_by_ts: dict[str, list] = {}
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            ts = row.get("poll_timestamp", "")
            rows_by_ts.setdefault(ts, []).append(row)

    snapshots = []
    for ts in sorted(rows_by_ts):
        vessels = []
        for row in rows_by_ts[ts]:
            v = {k: v for k, v in row.items() if k != "poll_timestamp"}
            # Coerce numeric fields
            for field in ("LATITUDE", "LONGITUDE", "SOG", "COG", "HEADING",
                          "ROT", "TYPE", "NAVSTAT", "DRAUGHT",
                          "IMO", "A", "B", "C", "D"):
                try:
                    v[field] = float(v[field]) if v[field] not in ("", None) else None
                except (ValueError, TypeError):
                    v[field] = None
            vessels.append(v)
        snapshots.append({"timestamp": ts, "vessels": vessels})
    return snapshots


def parse_ts(iso: str) -> int:
    """Return milliseconds since epoch from an ISO 8601 string."""
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return int(dt.timestamp() * 1000)
    except Exception:
        return 0


def build_payload(snapshots: list[dict], source_file: str) -> dict:
    """
    Compact the raw snapshots into a structure the animation engine can use:

      {
        "source":    "ais_20260405_120000.jsonl",
        "snapshots": [
          {
            "ts": 1712345678000,   # ms since epoch
            "vessels": {
              "123456789": [lat, lon, sog, cog, hdg, type, "NAME", "DEST", navstat]
            }
          }, ...
        ],
        "meta": { "vessel_names": {"123456789": "EVER GIVEN", ...},
                  "vessel_types": {"123456789": 71, ...} }
      }
    """
    compact_snaps = []
    all_names  = {}   # mmsi -> latest name
    all_types  = {}   # mmsi -> type int
    all_details = {}  # mmsi -> latest full detail dict (for popup)

    for snap in snapshots:
        ts = parse_ts(snap.get("timestamp", ""))
        if ts == 0:
            continue
        vessels_raw = snap.get("vessels", [])
        compact_v = {}
        for v in vessels_raw:
            try:
                mmsi = str(int(float(v.get("MMSI") or 0)))
                if mmsi == "0":
                    continue
                lat  = float(v.get("LATITUDE")  or 0)
                lon  = float(v.get("LONGITUDE") or 0)
                if lat == 0 and lon == 0:
                    continue
                sog  = _f(v.get("SOG"))
                cog  = _f(v.get("COG"))
                hdg  = _f(v.get("HEADING"))
                typ  = _i(v.get("TYPE"))
                name = str(v.get("NAME") or "").strip() or None
                dest = str(v.get("DEST") or "").strip() or None
                navs = _i(v.get("NAVSTAT"))

                compact_v[mmsi] = [lat, lon, sog, cog, hdg, typ, name, dest, navs]

                # keep richest metadata
                if name:
                    all_names[mmsi] = name
                if typ is not None:
                    all_types[mmsi] = typ
                all_details[mmsi] = {
                    "mmsi": mmsi,
                    "name": name,
                    "callsign": str(v.get("CALLSIGN") or "").strip() or None,
                    "imo":  _i(v.get("IMO")),
                    "type": typ,
                    "draught": _f(v.get("DRAUGHT")),
                    "dest": dest,
                    "eta":  str(v.get("ETA") or "").strip() or None,
                    "len":  _i(v.get("A", 0) or 0) + _i(v.get("B", 0) or 0),
                }
            except Exception:
                continue

        if compact_v:
            compact_snaps.append({"ts": ts, "v": compact_v})

    compact_snaps.sort(key=lambda s: s["ts"])

    return {
        "source":    os.path.basename(source_file),
        "snapshots": compact_snaps,
        "details":   all_details,
    }


def _f(v):
    try:
        return round(float(v), 3) if v not in (None, "", "None") else None
    except (ValueError, TypeError):
        return None


def _i(v):
    try:
        return int(float(v)) if v not in (None, "", "None") else None
    except (ValueError, TypeError):
        return None


# ── HTML template ──────────────────────────────────────────────────────────────

_HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>AIS Playback – {{SOURCE}}</title>
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.min.css"/>
<script src="https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.min.js"></script>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:'SF Mono','Consolas','Menlo',monospace;background:#0a0e14;color:#c4cad4;overflow:hidden}
#map{width:100%;height:calc(100vh - 58px)}

/* ── bottom toolbar ── */
#toolbar{
  position:fixed;bottom:0;left:0;right:0;height:58px;z-index:2000;
  background:rgba(8,12,18,.97);border-top:1px solid #1e2a3a;
  display:flex;align-items:center;gap:10px;padding:0 14px;
  backdrop-filter:blur(10px);
}
.tb-btn{
  background:#1a2535;border:1px solid #2a3a4a;border-radius:5px;
  color:#7eb8da;font-size:16px;width:34px;height:34px;cursor:pointer;
  display:flex;align-items:center;justify-content:center;flex-shrink:0;
  transition:background .15s;user-select:none;
}
.tb-btn:hover{background:#243040}
.tb-btn.active{background:#1a3d5a;border-color:#3a7ea8;color:#a0d4f0}
#scrubber{
  flex:1;-webkit-appearance:none;appearance:none;height:5px;
  border-radius:3px;outline:none;cursor:pointer;
  background:linear-gradient(to right,#2a6a9a 0%,#2a6a9a var(--pct,0%),#1e2a3a var(--pct,0%),#1e2a3a 100%);
}
#scrubber::-webkit-slider-thumb{-webkit-appearance:none;width:14px;height:14px;border-radius:50%;background:#7eb8da;cursor:pointer;box-shadow:0 0 4px rgba(126,184,218,.5)}
#scrubber::-moz-range-thumb{width:14px;height:14px;border-radius:50%;background:#7eb8da;border:none;cursor:pointer}
#time-display{font-size:13px;color:#a0c8e0;font-weight:600;min-width:175px;flex-shrink:0;letter-spacing:.3px}
#speed-select{
  background:#1a2535;border:1px solid #2a3a4a;border-radius:5px;
  color:#7eb8da;font-family:inherit;font-size:11px;padding:5px 8px;
  cursor:pointer;flex-shrink:0;outline:none;
}
#speed-select:focus{border-color:#3a7ea8}
#snap-indicator{font-size:10px;color:#3a5570;flex-shrink:0;min-width:80px;text-align:right}

/* ── info panel ── */
#info{
  position:fixed;top:12px;right:12px;z-index:1000;
  background:rgba(8,12,18,.93);border:1px solid #1e2a3a;
  border-radius:8px;padding:13px;width:270px;
  backdrop-filter:blur(12px);
}
#info h2{font-size:12px;font-weight:600;color:#7eb8da;margin-bottom:9px;letter-spacing:.3px}
.irow{display:flex;justify-content:space-between;font-size:10px;line-height:1.9}
.irow .lbl{color:#6b7785}
.irow .val{color:#c4cad4;font-weight:600;text-align:right;max-width:160px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.sep{border:none;border-top:1px solid #1e2a3a;margin:7px 0}
.legend{display:grid;grid-template-columns:1fr 1fr;gap:3px 10px;font-size:9px;margin-top:6px}
.li{display:flex;align-items:center;gap:5px;color:#6b7785}
.sw{width:9px;height:9px;border-radius:2px;flex-shrink:0}

/* ── popup ── */
.leaflet-popup-content-wrapper{background:rgba(8,12,18,.96)!important;border:1px solid #1e2a3a!important;border-radius:6px!important;box-shadow:0 4px 20px rgba(0,0,0,.7)!important}
.leaflet-popup-tip{background:rgba(8,12,18,.96)!important}
.leaflet-popup-content{margin:10px 13px!important;font-family:'SF Mono','Consolas','Menlo',monospace!important;font-size:11px!important;color:#c4cad4!important}
.pt{font-size:13px;font-weight:700;color:#7eb8da;margin-bottom:7px}
.pg{display:grid;grid-template-columns:auto 1fr;gap:2px 10px;font-size:10px}
.pk{color:#6b7785}.pv{color:#c4cad4;font-weight:600}

/* ── speed indicator overlay ── */
#speed-flash{
  position:fixed;top:50%;left:50%;transform:translate(-50%,-50%);
  font-size:48px;font-weight:900;color:rgba(126,184,218,.7);
  pointer-events:none;opacity:0;transition:opacity .3s;z-index:3000;
}
</style>
</head>
<body>

<div id="map"></div>
<div id="speed-flash"></div>

<!-- info panel -->
<div id="info">
  <h2>&#9875; AIS Playback</h2>
  <div class="irow"><span class="lbl">File</span>       <span class="val" id="i-file">—</span></div>
  <div class="irow"><span class="lbl">Duration</span>   <span class="val" id="i-dur">—</span></div>
  <div class="irow"><span class="lbl">Snapshots</span>  <span class="val" id="i-snaps">—</span></div>
  <hr class="sep">
  <div class="irow"><span class="lbl">Current time</span> <span class="val" id="i-time">—</span></div>
  <div class="irow"><span class="lbl">Snapshot</span>     <span class="val" id="i-cur">—</span></div>
  <div class="irow"><span class="lbl">Vessels visible</span><span class="val" id="i-count">—</span></div>
  <hr class="sep">
  <div class="legend">
    <div class="li"><div class="sw" style="background:#e05555"></div>Tanker</div>
    <div class="li"><div class="sw" style="background:#3a9a50"></div>Cargo</div>
    <div class="li"><div class="sw" style="background:#3a7ae0"></div>Passenger</div>
    <div class="li"><div class="sw" style="background:#d4a030"></div>Fishing</div>
    <div class="li"><div class="sw" style="background:#9a50c0"></div>Tug/Pilot</div>
    <div class="li"><div class="sw" style="background:#d46020"></div>High-speed</div>
    <div class="li"><div class="sw" style="background:#30a0a0"></div>Sail/Leisure</div>
    <div class="li"><div class="sw" style="background:#607080"></div>Other</div>
  </div>
</div>

<!-- bottom toolbar -->
<div id="toolbar">
  <button class="tb-btn" id="btn-first"  title="Jump to start"    onclick="jumpToStart()">&#8676;</button>
  <button class="tb-btn" id="btn-prev"   title="Previous snapshot" onclick="stepSnap(-1)">&#9664;</button>
  <button class="tb-btn" id="btn-play"   title="Play / Pause (Space)" onclick="togglePlay()">&#9654;</button>
  <button class="tb-btn" id="btn-next"   title="Next snapshot"    onclick="stepSnap(1)">&#9654;&#9654;</button>
  <button class="tb-btn" id="btn-last"   title="Jump to end"      onclick="jumpToEnd()">&#8677;</button>
  <input  type="range" id="scrubber" min="0" max="1000" value="0"
          oninput="onScrub(this.value)" onchange="onScrub(this.value)"/>
  <span id="time-display">—</span>
  <select id="speed-select" onchange="setSpeed(this.value)">
    <option value="1">1× speed</option>
    <option value="5">5×</option>
    <option value="10" selected>10×</option>
    <option value="30">30×</option>
    <option value="60">60×</option>
    <option value="120">120×</option>
    <option value="300">300×</option>
  </select>
  <span id="snap-indicator">—</span>
</div>

<script>
// ── Injected data ──────────────────────────────────────────────────────────
const DATA = {{INJECT_DATA}};

// ── Constants ──────────────────────────────────────────────────────────────
const TRAIL_LEN   = 8;    // number of past snapshots to show as trail
const STALE_SNAPS = 3;    // hide vessel if not seen in this many snapshots
const FPS_CAP     = 40;   // max DOM updates per second

// V array indices
const I_LAT=0, I_LON=1, I_SOG=2, I_COG=3, I_HDG=4, I_TYP=5, I_NAM=6, I_DST=7, I_NST=8;

// ── Map ────────────────────────────────────────────────────────────────────
const map = L.map('map', {
  center:[50.0,-1.5], zoom:8,
  renderer: L.canvas({ padding:0.5 }),
});
L.tileLayer('https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png',{
  attribution:'&copy; OpenStreetMap &copy; CARTO', maxZoom:18,
}).addTo(map);

// ── Vessel colours / labels ────────────────────────────────────────────────
function shipColour(type){
  const t=Number(type)||0;
  if(t>=80&&t<=89) return '#e05555';
  if(t>=70&&t<=79) return '#3a9a50';
  if(t>=60&&t<=69) return '#3a7ae0';
  if(t===30)        return '#d4a030';
  if([21,22,31,32,50,51,52,53,54,55,56,57,58,59].includes(t)) return '#9a50c0';
  if(t>=40&&t<=49)  return '#d46020';
  if(t===36||t===37)return '#30a0a0';
  return '#607080';
}
function shipLabel(type){
  const t=Number(type)||0;
  if(t>=80&&t<=89) return 'Tanker';
  if(t>=70&&t<=79) return 'Cargo';
  if(t>=60&&t<=69) return 'Passenger';
  if(t===30)        return 'Fishing';
  if(t>=50&&t<=59)  return 'Special craft';
  if(t===31||t===32)return 'Tug';
  if(t>=40&&t<=49)  return 'High-speed';
  if(t===36||t===37)return 'Sail/Leisure';
  return `Type ${t}`;
}
const NAVSTAT_LABELS=['Under way (engine)','At anchor','Not under command',
  'Restricted manoeuvrability','Constrained by draught','Moored','Aground',
  'Engaged in fishing','Under way (sailing)'];

// ── Arrow icon ─────────────────────────────────────────────────────────────
const iconCache={};
function arrowIcon(colour,heading){
  const h=(isNaN(heading)||heading===511||heading===null)?0:Math.round(heading/5)*5;
  const key=colour+'_'+h;
  if(iconCache[key]) return iconCache[key];
  const svg=`<svg xmlns="http://www.w3.org/2000/svg" width="22" height="22" viewBox="-11 -11 22 22">
    <g transform="rotate(${h})">
      <polygon points="0,-10 6,6 0,2 -6,6" fill="${colour}" stroke="rgba(0,0,0,.55)" stroke-width="1.2"/>
    </g></svg>`;
  const icon=L.divIcon({html:svg,className:'',iconSize:[22,22],iconAnchor:[11,11]});
  iconCache[key]=icon;
  return icon;
}

// ── Interpolation helpers ──────────────────────────────────────────────────
function lerp(a,b,f){ return a+(b-a)*f; }
function lerpAngle(a,b,f){
  if(a===null||a===undefined) return b;
  if(b===null||b===undefined) return a;
  let diff=((b-a+540)%360)-180;
  return (a+diff*f+360)%360;
}

// ── Pre-compute per-vessel snapshot history for trails ─────────────────────
// vesselSnaps[mmsi] = [{snapIdx, lat, lon}]  sorted by snapIdx
const vesselSnaps={};
DATA.snapshots.forEach((snap,si)=>{
  Object.entries(snap.v).forEach(([mmsi,v])=>{
    if(!vesselSnaps[mmsi]) vesselSnaps[mmsi]=[];
    vesselSnaps[mmsi].push({si, lat:v[I_LAT], lon:v[I_LON]});
  });
});

// ── Playback state ─────────────────────────────────────────────────────────
const snaps     = DATA.snapshots;
const firstTs   = snaps[0].ts;
const lastTs    = snaps[snaps.length-1].ts;
const totalMs   = lastTs - firstTs;

const pb = {
  playing:  false,
  speed:    10,
  simTime:  firstTs,   // ms
  wallPrev: null,
  snapIdx:  0,
};

// ── Marker / trail stores ──────────────────────────────────────────────────
const markers={};   // mmsi -> L.marker
const trails={};    // mmsi -> L.polyline
const lastSeen={};  // mmsi -> snapIdx when last visible

// ── Animation loop ─────────────────────────────────────────────────────────
let lastDomMs = 0;

function rafLoop(wallNow){
  requestAnimationFrame(rafLoop);

  // Advance simulation time
  if(pb.playing && pb.wallPrev!==null){
    pb.simTime += (wallNow - pb.wallPrev) * pb.speed;
    if(pb.simTime >= lastTs){
      pb.simTime = lastTs;
      pb.playing = false;
      document.getElementById('btn-play').innerHTML='&#9654;';
    }
  }
  pb.wallPrev = wallNow;

  // Cap DOM renders
  if(wallNow - lastDomMs < 1000/FPS_CAP) return;
  lastDomMs = wallNow;

  // Find current snapshot bracket
  let si = pb.snapIdx;
  while(si < snaps.length-1 && snaps[si+1].ts <= pb.simTime) si++;
  while(si > 0 && snaps[si].ts > pb.simTime) si--;
  pb.snapIdx = si;

  const snap1 = snaps[si];
  const snap2 = si < snaps.length-1 ? snaps[si+1] : snap1;
  const spanMs = snap2.ts - snap1.ts;
  const f = spanMs > 0 ? Math.max(0, Math.min(1, (pb.simTime - snap1.ts)/spanMs)) : 0;

  // All MMSIs visible around this snapshot
  const window1 = Math.max(0, si - STALE_SNAPS);
  const activeMMSIs = new Set();
  for(let k=window1; k<=Math.min(snaps.length-1, si+1); k++){
    Object.keys(snaps[k].v).forEach(m=>activeMMSIs.add(m));
  }

  // Update / create markers
  activeMMSIs.forEach(mmsi=>{
    const v1 = snap1.v[mmsi];
    const v2 = snap2.v[mmsi];
    let lat, lon, hdg, sog, type, name;

    if(v1 && v2){
      lat = lerp(v1[I_LAT], v2[I_LAT], f);
      lon = lerp(v1[I_LON], v2[I_LON], f);
      hdg = lerpAngle(v1[I_HDG], v2[I_HDG], f);
      sog = lerp(v1[I_SOG]||0, v2[I_SOG]||0, f);
      type= v1[I_TYP];
      name= v1[I_NAM] || v2[I_NAM];
    } else if(v1){
      lat=v1[I_LAT]; lon=v1[I_LON]; hdg=v1[I_HDG]; sog=v1[I_SOG]; type=v1[I_TYP]; name=v1[I_NAM];
    } else if(v2){
      lat=v2[I_LAT]; lon=v2[I_LON]; hdg=v2[I_HDG]; sog=v2[I_SOG]; type=v2[I_TYP]; name=v2[I_NAM];
    } else { return; }

    const col  = shipColour(type);
    const icon = arrowIcon(col, hdg);

    if(!markers[mmsi]){
      const m = L.marker([lat,lon],{icon, zIndexOffset:100}).addTo(map);
      m.on('click', ()=>openPopup(mmsi, lat, lon, v1||v2));
      markers[mmsi]=m;
      // thin trail polyline
      trails[mmsi] = L.polyline([], {
        color: col, weight:1.5, opacity:0.45, smoothFactor:1,
      }).addTo(map);
    } else {
      markers[mmsi].setLatLng([lat,lon]);
      markers[mmsi].setIcon(icon);
      // re-attach click with current data
      markers[mmsi].off('click');
      const vSnap = v1||v2;
      markers[mmsi].on('click', ()=>openPopup(mmsi, lat, lon, vSnap));
    }
    lastSeen[mmsi] = si;

    // Trail: last TRAIL_LEN confirmed positions
    const history = vesselSnaps[mmsi];
    if(history){
      const trailPts=[];
      for(let ti=history.length-1; ti>=0; ti--){
        const h=history[ti];
        if(h.si > si) continue;
        if(trailPts.length >= TRAIL_LEN) break;
        trailPts.unshift([h.lat, h.lon]);
      }
      // add current interpolated position
      trailPts.push([lat,lon]);
      trails[mmsi].setLatLngs(trailPts);
    }
  });

  // Remove stale markers
  Object.keys(markers).forEach(mmsi=>{
    if(!activeMMSIs.has(mmsi)){
      map.removeLayer(markers[mmsi]);
      map.removeLayer(trails[mmsi]);
      delete markers[mmsi];
      delete trails[mmsi];
    }
  });

  // Update UI
  updateUI(si, activeMMSIs.size);
}

// ── UI updates ─────────────────────────────────────────────────────────────
function updateUI(si, count){
  const pct = totalMs>0 ? ((pb.simTime-firstTs)/totalMs)*100 : 0;
  const scrub = document.getElementById('scrubber');
  scrub.value = pct*10; // max=1000
  scrub.style.setProperty('--pct', pct.toFixed(2)+'%');

  const dt = new Date(pb.simTime);
  document.getElementById('time-display').textContent =
    dt.toLocaleDateString('en-GB',{day:'2-digit',month:'short',year:'numeric'}) + '  ' +
    dt.toLocaleTimeString('en-GB',{hour:'2-digit',minute:'2-digit',second:'2-digit'}) + ' UTC';

  document.getElementById('i-time').textContent  = dt.toISOString().replace('T',' ').slice(0,-5)+' Z';
  document.getElementById('i-cur').textContent   = `${si+1} / ${snaps.length}`;
  document.getElementById('i-count').textContent = count;
  document.getElementById('snap-indicator').textContent = `snap ${si+1}/${snaps.length}`;
}

// ── Controls ───────────────────────────────────────────────────────────────
function togglePlay(){
  pb.playing = !pb.playing;
  document.getElementById('btn-play').innerHTML = pb.playing ? '&#9646;&#9646;' : '&#9654;';
  if(pb.playing && pb.simTime >= lastTs){ pb.simTime = firstTs; pb.snapIdx=0; }
}
function jumpToStart(){ pb.simTime=firstTs; pb.snapIdx=0; }
function jumpToEnd()  { pb.simTime=lastTs;  pb.snapIdx=snaps.length-1; }
function stepSnap(dir){
  pb.playing=false;
  document.getElementById('btn-play').innerHTML='&#9654;';
  const target = Math.max(0, Math.min(snaps.length-1, pb.snapIdx+dir));
  pb.simTime  = snaps[target].ts;
  pb.snapIdx  = target;
}
function onScrub(v){
  pb.playing=false;
  document.getElementById('btn-play').innerHTML='&#9654;';
  pb.simTime = firstTs + (v/1000)*totalMs;
  pb.snapIdx = 0; // will be corrected in rafLoop
}
function setSpeed(v){
  pb.speed = Number(v);
  flashSpeed(v+'×');
}
function flashSpeed(msg){
  const el=document.getElementById('speed-flash');
  el.textContent=msg; el.style.opacity=1;
  setTimeout(()=>el.style.opacity=0, 600);
}

// ── Popup ──────────────────────────────────────────────────────────────────
function openPopup(mmsi, lat, lon, v){
  const det = DATA.details[mmsi] || {};
  const name    = (v&&v[I_NAM]) || det.name || 'Unknown';
  const type    = (v&&v[I_TYP]) ?? det.type;
  const sog     = (v&&v[I_SOG]!=null) ? v[I_SOG].toFixed(1)+' kn' : '—';
  const cog     = (v&&v[I_COG]!=null) ? v[I_COG].toFixed(1)+'°'  : '—';
  const hdg     = (v&&v[I_HDG]!==null&&v[I_HDG]!==511) ? v[I_HDG]+'°' : '—';
  const dest    = (v&&v[I_DST]) || det.dest || '—';
  const nsIdx   = (v&&v[I_NST]!=null) ? v[I_NST] : -1;
  const ns      = NAVSTAT_LABELS[nsIdx] || '—';
  const draught = det.draught != null ? det.draught+'m' : '—';
  const eta     = det.eta || '—';
  const imo     = det.imo || '—';
  const cs      = det.callsign || '—';
  const len     = det.len > 0 ? det.len+'m' : '—';

  const html=`<div class="pt">${name}</div>
<div class="pg">
  <span class="pk">MMSI</span>      <span class="pv">${mmsi}</span>
  <span class="pk">IMO</span>       <span class="pv">${imo}</span>
  <span class="pk">Call sign</span> <span class="pv">${cs}</span>
  <span class="pk">Type</span>      <span class="pv">${shipLabel(type)} (${type??'?'})</span>
  <span class="pk">Status</span>    <span class="pv">${ns}</span>
  <span class="pk">Speed</span>     <span class="pv">${sog}</span>
  <span class="pk">Course</span>    <span class="pv">${cog}</span>
  <span class="pk">Heading</span>   <span class="pv">${hdg}</span>
  <span class="pk">Draught</span>   <span class="pv">${draught}</span>
  <span class="pk">Length</span>    <span class="pv">${len}</span>
  <span class="pk">Destination</span><span class="pv">${dest}</span>
  <span class="pk">ETA</span>       <span class="pv">${eta}</span>
  <span class="pk">Position</span>  <span class="pv">${lat.toFixed(4)}° / ${lon.toFixed(4)}°</span>
</div>`;
  L.popup({maxWidth:280}).setLatLng([lat,lon]).setContent(html).openOn(map);
}

// ── Keyboard shortcuts ─────────────────────────────────────────────────────
document.addEventListener('keydown', e=>{
  if(e.target.tagName==='INPUT'||e.target.tagName==='SELECT') return;
  if(e.code==='Space')     { e.preventDefault(); togglePlay(); }
  if(e.code==='ArrowLeft') { e.preventDefault(); stepSnap(-1); }
  if(e.code==='ArrowRight'){ e.preventDefault(); stepSnap(1); }
  if(e.code==='Home')      { e.preventDefault(); jumpToStart(); }
  if(e.code==='End')       { e.preventDefault(); jumpToEnd(); }
  const speedKeys={'Digit1':1,'Digit2':5,'Digit3':10,'Digit4':30,'Digit5':60,'Digit6':120};
  if(speedKeys[e.code]){
    pb.speed=speedKeys[e.code];
    document.getElementById('speed-select').value=pb.speed;
    flashSpeed(pb.speed+'×');
  }
});

// ── Init ───────────────────────────────────────────────────────────────────
(function init(){
  const fmtDur = ms=>{
    const s=Math.round(ms/1000);
    const h=Math.floor(s/3600), m=Math.floor((s%3600)/60), ss=s%60;
    return h>0 ? `${h}h ${m}m` : m>0 ? `${m}m ${ss}s` : `${ss}s`;
  };

  document.getElementById('i-file').textContent  = DATA.source;
  document.getElementById('i-dur').textContent   = fmtDur(totalMs);
  document.getElementById('i-snaps').textContent = snaps.length;

  requestAnimationFrame(rafLoop);
})();
</script>
</body>
</html>
"""


# ── HTML generation ────────────────────────────────────────────────────────────

def generate_html(payload: dict, source_file: str) -> str:
    # Escape </script> inside the JSON to prevent HTML parser confusion
    data_json = json.dumps(payload, ensure_ascii=False, separators=(',', ':'))
    data_json = data_json.replace("</", r"<\/")

    html = _HTML_TEMPLATE
    html = html.replace("{{SOURCE}}", os.path.basename(source_file))
    html = html.replace("{{INJECT_DATA}}", data_json)
    return html


# ── CLI ────────────────────────────────────────────────────────────────────────

def find_latest_file() -> str | None:
    patterns = [
        os.path.join(DATA_DIR, "*.jsonl"),
        os.path.join(DATA_DIR, "*.csv"),
    ]
    candidates = []
    for pat in patterns:
        candidates.extend(glob.glob(pat))
    if not candidates:
        return None
    return max(candidates, key=os.path.getmtime)


def list_files():
    patterns = [os.path.join(DATA_DIR, "*.jsonl"), os.path.join(DATA_DIR, "*.csv")]
    files = []
    for pat in patterns:
        files.extend(glob.glob(pat))
    if not files:
        print(f"No recording files found in {DATA_DIR}/")
        return
    files.sort(key=os.path.getmtime, reverse=True)
    print(f"\nAvailable recordings in {DATA_DIR}/:")
    for f in files:
        size_kb = os.path.getsize(f) / 1024
        mtime   = datetime.fromtimestamp(os.path.getmtime(f)).strftime("%Y-%m-%d %H:%M")
        print(f"  {os.path.basename(f):<40}  {size_kb:6.0f} KB   {mtime}")
    print()


def main():
    args = sys.argv[1:]

    if "--list" in args or "-l" in args:
        list_files()
        return

    if args:
        source = args[0]
    else:
        source = find_latest_file()
        if not source:
            print(f"Error: no recording files found in {DATA_DIR}/")
            print("Run aishub_tracker.py first to record some data.")
            sys.exit(1)
        print(f"Auto-selected: {source}")

    if not os.path.exists(source):
        print(f"Error: file not found: {source}")
        sys.exit(1)

    ext = os.path.splitext(source)[1].lower()
    print(f"Loading {os.path.basename(source)} …")

    if ext == ".jsonl":
        raw = load_jsonl(source)
    elif ext == ".csv":
        raw = load_csv(source)
    else:
        print(f"Error: unsupported file type '{ext}' — expected .jsonl or .csv")
        sys.exit(1)

    if not raw:
        print("Error: file is empty or contains no valid records.")
        sys.exit(1)

    print(f"  {len(raw)} snapshots loaded")

    payload = build_payload(raw, source)
    n_snaps = len(payload["snapshots"])
    if n_snaps == 0:
        print("Error: no usable snapshot data found in file.")
        sys.exit(1)

    # Count unique vessels across all snapshots
    all_mmsi = set()
    for s in payload["snapshots"]:
        all_mmsi.update(s["v"].keys())

    first_dt = datetime.fromtimestamp(payload["snapshots"][0]["ts"] / 1000, tz=timezone.utc)
    last_dt  = datetime.fromtimestamp(payload["snapshots"][-1]["ts"] / 1000, tz=timezone.utc)
    dur_min  = (payload["snapshots"][-1]["ts"] - payload["snapshots"][0]["ts"]) / 60000

    print(f"  {n_snaps} valid snapshots")
    print(f"  {len(all_mmsi)} unique vessels")
    print(f"  {first_dt.strftime('%Y-%m-%d %H:%M')} → {last_dt.strftime('%H:%M')} UTC  ({dur_min:.0f} min)")

    html = generate_html(payload, source)

    # Write output next to the source file (or in DATA_DIR)
    base = os.path.splitext(os.path.basename(source))[0]
    out_dir = os.path.dirname(os.path.abspath(source))
    out_path = os.path.join(out_dir, f"{base}_playback.html")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html)

    size_kb = os.path.getsize(out_path) / 1024
    print(f"\nGenerated: {out_path}  ({size_kb:.0f} KB)")
    print("Opening in browser…")
    webbrowser.open(f"file://{os.path.abspath(out_path)}")


if __name__ == "__main__":
    main()
