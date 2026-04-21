# English Channel Live AIS Tracker

Real-time ship tracking maps for the English Channel using AIS (Automatic Identification System) data. Two data sources are supported:

- **AISHub** (`aishub_tracker.py`) — REST polling API, records data to file
- **aisstream.io** (`ais_proxy.py`) — WebSocket stream, live updates only

## What's Included

| File | Description |
|------|-------------|
| `aishub_tracker.py` | AISHub REST tracker — self-contained server + map viewer with recording |
| `ais_proxy.py` | aisstream.io WebSocket proxy — required for the HTML maps below |
| `english_channel_ais.html` | All vessel traffic (tankers, cargo, passenger, fishing, etc.) |
| `english_channel_state_vessels.html` | State vessels only (military, SAR/lifeboats, police, pilot, coastguard) |

---

## AISHub Tracker (`aishub_tracker.py`)

Polls the AISHub REST API once per minute, renders vessels on an interactive Leaflet map, and continuously records position data to disk.

### Features

- **Rate-limited** — never exceeds the AISHub 1 request/minute limit
- **Recording** — every poll snapshot is appended to a timestamped `.jsonl` file and a `.csv` file in the `ais_data/` directory
- **Pause / resume** recording from the browser without stopping the tracker
- **Download CSV or JSONL** of the current session directly from the map page
- **Poll history** panel shows the vessel count for every poll this session
- Click any vessel to see full AIS details (MMSI, name, type, speed, course, heading, draught, destination, ETA)

### Quick start

No extra packages required — uses Python standard library only.

```bash
python3 aishub_tracker.py
```

Then open **http://localhost:8082** in your browser.

Terminal output:

```
────────────────────────────────────────────────────
  AISHub English Channel Tracker
────────────────────────────────────────────────────
  API key   : AH_2670_9F0D1564
  Bounding  : 48.3°N–51.5°N  -6.0°E–2.5°E
  Interval  : 60 s  (API rate limit)
  Data dir  : /path/to/ais_data/
  Viewer    : http://localhost:8082
────────────────────────────────────────────────────

Open http://localhost:8082 in your browser.
Press Ctrl+C to stop.

[12:00:00] Polling AISHub API…
  ↳ 312 vessels  |  poll #1
[12:01:00] Polling AISHub API…
  ↳ 318 vessels  |  poll #2
```

### Recorded files

Each run creates two files in `ais_data/`:

| File | Format | Contents |
|------|--------|----------|
| `ais_YYYYMMDD_HHMMSS.jsonl` | JSONL | One JSON object per poll — full vessel list + metadata |
| `ais_YYYYMMDD_HHMMSS.csv`   | CSV   | One row per vessel per poll — all AIS fields |

The CSV columns are: `poll_timestamp`, `MMSI`, `NAME`, `CALLSIGN`, `TYPE`, `LATITUDE`, `LONGITUDE`, `SOG`, `COG`, `HEADING`, `NAVSTAT`, `ROT`, `IMO`, `DRAUGHT`, `DEST`, `ETA`, `A`, `B`, `C`, `D`.

### REST API

The tracker also exposes a local API for the map and for scripted access:

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/` | GET | Embedded map viewer |
| `/api/vessels` | GET | Current vessel snapshot (JSON) |
| `/api/status` | GET | Polling status, recording state, file paths |
| `/api/history` | GET | Summary of every poll this session |
| `/api/export/csv` | GET | Download current session CSV |
| `/api/export/json` | GET | Download current session JSONL |
| `/api/recording/start` | POST | Resume recording |
| `/api/recording/stop` | POST | Pause recording |

---

## aisstream.io WebSocket Tracker

## Requirements

- Python 3.8+
- The `websockets` Python package
- A modern web browser (Chrome, Firefox, Safari)
- An internet connection

## Setup

### 1. Install the websockets package

```bash
pip install websockets
```

### 2. Start the proxy

Open a terminal, navigate to the folder containing these files, and run:

```bash
python3 ais_proxy.py
```

Leave this running. You should see:

```
AIS WebSocket Proxy
  Local:    ws://localhost:9000
  Upstream: wss://stream.aisstream.io/v0/stream
  Area:     English Channel (48.3N-51.5N, 6W-2.5E)

Waiting for browser connections...
──────────────────────────────────────────────────
```

### 3. Serve the HTML files

Open a second terminal in the same folder and run:

```bash
python3 -m http.server 8080
```

### 4. Open in browser

- **All traffic:** http://localhost:8080/english_channel_ais.html
- **State vessels:** http://localhost:8080/english_channel_state_vessels.html

Click **Connect** to start streaming.

## How It Works

```
aisstream.io (AIS data) ──WebSocket──▶ ais_proxy.py (localhost:9000) ──WebSocket──▶ Browser map
```

The proxy is needed because aisstream.io's WebSocket server sends non-standard responses that browsers reject. Python handles the upstream connection, converts binary frames to text, and serves them to the browser over a clean local WebSocket.

## The Maps

### All Traffic (`english_channel_ais.html`)

Shows every vessel broadcasting AIS in the English Channel. Ships appear as directional arrows, colour-coded by type:

- **Red** — Tanker
- **Green** — Cargo
- **Blue** — Passenger
- **Yellow** — Fishing
- **Purple** — Tug / Pilot
- **Orange** — High-speed craft
- **Teal** — Sailing / Pleasure
- **Grey** — Other

### State Vessels (`english_channel_state_vessels.html`)

Filters all incoming AIS traffic and only shows government/state-operated vessels. Filtering uses three methods:

1. **AIS type codes** — 35 (military), 50 (pilot), 51 (SAR), 53 (port tender), 55 (law enforcement), 58 (medical)
2. **MMSI prefix** — 111xxxxx (SAR aircraft), 970xxxxx (SART beacons)
3. **Vessel name keywords** — RNLI, KNRM, COASTGUARD, KUSTWACHT, POLITIE, DOUANE, BORDER FORCE, HMS, HNLMS, MARINE NATIONALE, LOODS, etc.

State vessels are rarer than commercial traffic — expect a minute or two before the first ones appear. The stats panel shows both the number of state vessels on the map and the total number of all vessels being scanned.

## Interaction

- **Click any vessel** to see its details: name, MMSI, type, speed, course, heading, destination, ETA, draught, and position
- **Scroll to zoom**, drag to pan
- Vessels leave a faint **trail** as they move
- **Stale vessels** (no update in 5–10 minutes) are automatically removed

## Notes

- aisstream.io is a free service currently in beta — it may go down without notice. If the proxy terminal shows connection errors, the service may be temporarily unavailable.
- AIS coverage is terrestrial only (land-based receivers), so vessels far offshore may not appear.
- The bounding box covers the English Channel from the Western Approaches through the Strait of Dover (approximately 48.3°N to 51.5°N, 6°W to 2.5°E). To change the area, edit the `BBOX_SW` and `BBOX_NE` values in the HTML file and the `SUBSCRIPTION` dict in `ais_proxy.py`.
- The API key is pre-filled. If it stops working, generate a new one at https://aisstream.io (sign in with GitHub).

## Stopping

- Press `Ctrl+C` in each terminal window to stop the proxy and the HTTP server.
