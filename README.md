# English Channel Live AIS Tracker

Real-time ship tracking maps for the English Channel using AIS (Automatic Identification System) data from aisstream.io. Two views are included: all vessel traffic, and a filtered view showing only state/government vessels.

## What's Included

| File | Description |
|------|-------------|
| `ais_proxy.py` | Local WebSocket proxy — required for both maps |
| `english_channel_ais.html` | All vessel traffic (tankers, cargo, passenger, fishing, etc.) |
| `english_channel_state_vessels.html` | State vessels only (military, SAR/lifeboats, police, pilot, coastguard) |

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
