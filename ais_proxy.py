#!/usr/bin/env python3
"""
AIS WebSocket Proxy
Bridges browser ↔ aisstream.io since their server sends
non-standard WebSocket responses that browsers reject.

Usage:
  pip install websockets
  python ais_proxy.py

Then open the HTML map and it connects to ws://localhost:9000
"""

import asyncio
import websockets
import json

UPSTREAM = "wss://stream.aisstream.io/v0/stream"
LOCAL_PORT = 9000

API_KEY = "69d038ee63578ddc6d01327665ac6c73f1852596"

# English Channel
SUBSCRIPTION = {
    "APIKey": API_KEY,
    "BoundingBoxes": [[[48.3, -6.0], [51.5, 2.5]]]
}

async def proxy(browser_ws):
    """One browser client connected — open upstream and bridge."""
    addr = browser_ws.remote_address
    print(f"[+] Browser connected from {addr}")

    # Wait for optional subscription override from browser
    try:
        raw = await asyncio.wait_for(browser_ws.recv(), timeout=2)
        sub = json.loads(raw)
        sub["APIKey"] = API_KEY
        print(f"    Using browser subscription: {json.dumps(sub)[:120]}...")
    except (asyncio.TimeoutError, Exception):
        sub = SUBSCRIPTION
        print(f"    Using default subscription (English Channel)")

    upstream_ws = None
    try:
        print(f"    Connecting to {UPSTREAM}...")
        upstream_ws = await websockets.connect(UPSTREAM, ping_interval=None)
        print(f"    Upstream connected. Sending subscription...")
        await upstream_ws.send(json.dumps(sub))
        print(f"    Streaming data to browser...")

        count = 0
        async for msg in upstream_ws:
            try:
                # aisstream.io sends binary frames — decode to text for browser
                if isinstance(msg, bytes):
                    msg = msg.decode('utf-8')

                await browser_ws.send(msg)
                count += 1
                if count <= 3:
                    print(f"    → msg #{count}: {msg[:150]}...")
                elif count % 500 == 0:
                    print(f"    → {count} messages forwarded")
            except websockets.ConnectionClosed:
                print(f"[-] Browser disconnected after {count} messages")
                break

    except websockets.ConnectionClosed as e:
        print(f"[!] Upstream closed: code={e.code} reason='{e.reason}'")
    except Exception as e:
        print(f"[!] Upstream error: {type(e).__name__}: {e}")
    finally:
        if upstream_ws:
            await upstream_ws.close()
        print(f"[-] Session ended ({addr})")

async def main():
    print(f"AIS WebSocket Proxy")
    print(f"  Local:    ws://localhost:{LOCAL_PORT}")
    print(f"  Upstream: {UPSTREAM}")
    print(f"  Area:     English Channel (48.3N-51.5N, 6W-2.5E)")
    print(f"")
    print(f"Waiting for browser connections...")
    print(f"─" * 50)

    async with websockets.serve(proxy, "localhost", LOCAL_PORT):
        await asyncio.Future()  # run forever

if __name__ == "__main__":
    asyncio.run(main())
