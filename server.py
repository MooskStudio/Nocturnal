#!/usr/bin/env python3
"""
AIS Map Server
Serves the HTML map files and proxies WebSocket data from aisstream.io.

Usage:
  python3 server.py        (or double-click start.command on macOS)

Opens http://localhost:8080/english_channel_ais.html automatically.
Press Ctrl+C to stop.
"""

import asyncio
import websockets
import json
import threading
import http.server
import webbrowser
import os
import sys

UPSTREAM  = "wss://stream.aisstream.io/v0/stream"
HTTP_PORT = 8080
WS_PORT   = 9000
API_KEY   = "69d038ee63578ddc6d01327665ac6c73f1852596"

DEFAULT_SUB = {
    "APIKey": API_KEY,
    "BoundingBoxes": [[[48.3, -6.0], [51.5, 2.5]]]
}

# ── WebSocket proxy ────────────────────────────────────────────────────

async def proxy(browser_ws):
    addr = browser_ws.remote_address
    print(f"  [ws]  browser connected from {addr}")

    try:
        raw = await asyncio.wait_for(browser_ws.recv(), timeout=2)
        sub = json.loads(raw)
        sub["APIKey"] = API_KEY
    except Exception:
        sub = DEFAULT_SUB

    upstream_ws = None
    try:
        upstream_ws = await websockets.connect(UPSTREAM, ping_interval=None)
        await upstream_ws.send(json.dumps(sub))
        count = 0
        async for msg in upstream_ws:
            try:
                if isinstance(msg, bytes):
                    msg = msg.decode("utf-8")
                await browser_ws.send(msg)
                count += 1
                if count % 500 == 0:
                    print(f"  [ws]  {count} msgs forwarded")
            except websockets.ConnectionClosed:
                break
    except Exception as e:
        print(f"  [ws]  upstream error: {e}")
    finally:
        if upstream_ws:
            await upstream_ws.close()
        print(f"  [ws]  session ended ({addr})")

async def ws_server():
    async with websockets.serve(proxy, "localhost", WS_PORT):
        await asyncio.Future()

# ── HTTP file server ───────────────────────────────────────────────────

class QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass

def http_server():
    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    with http.server.HTTPServer(("", HTTP_PORT), QuietHandler) as srv:
        srv.serve_forever()

# ── Entry point ────────────────────────────────────────────────────────

def main():
    print("AIS Map Server")
    print(f"  Maps:  http://localhost:{HTTP_PORT}/")
    print(f"  Proxy: ws://localhost:{WS_PORT}")
    print(f"  Press Ctrl+C to stop\n")

    threading.Thread(target=http_server, daemon=True).start()

    import time; time.sleep(0.4)
    webbrowser.open(f"http://localhost:{HTTP_PORT}/english_channel_ais.html")
    print(f"  Browser opened. Waiting for connections...\n")

    try:
        asyncio.run(ws_server())
    except KeyboardInterrupt:
        print("\nStopped.")
        sys.exit(0)

if __name__ == "__main__":
    main()
