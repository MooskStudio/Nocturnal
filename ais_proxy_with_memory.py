#!/usr/bin/env python3
"""
ais_proxy_with_memory.py — ais_proxy.py + Phase 1 persistence.

Drop-in replacement for ais_proxy.py. Same WebSocket bridge, same
behaviour for the browser — but every message is also written to a
local SQLite database via ais_store.AISStore.

Usage:
  pip install websockets
  python ais_proxy_with_memory.py
"""

import asyncio
import json
import os

import websockets  # type: ignore

from ais_store import AISStore


UPSTREAM   = "wss://stream.aisstream.io/v0/stream"
LOCAL_PORT = 9000

API_KEY = "69d038ee63578ddc6d01327665ac6c73f1852596"

# English Channel (unchanged from ais_proxy.py)
SUBSCRIPTION = {
    "APIKey": API_KEY,
    "BoundingBoxes": [[[48.3, -6.0], [51.5, 2.5]]],
}

# Shared DB path. Keep it alongside this script so it works the same way
# on macOS and Windows without hard-coded user paths.
DB_PATH = os.environ.get(
    "NOCTURNAL_DB",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "ais_memory.db"),
)

# Single, process-wide store. SQLite + our lock handles concurrency.
STORE = AISStore(DB_PATH)


async def proxy(browser_ws):
    addr = browser_ws.remote_address
    print(f"[+] Browser connected from {addr}")

    # Optional subscription override from browser (same protocol as before).
    try:
        raw = await asyncio.wait_for(browser_ws.recv(), timeout=2)
        sub = json.loads(raw)
        sub["APIKey"] = API_KEY
        print(f"    Using browser subscription: {json.dumps(sub)[:120]}...")
    except (asyncio.TimeoutError, Exception):
        sub = SUBSCRIPTION
        print("    Using default subscription (English Channel)")

    upstream_ws = None
    try:
        print(f"    Connecting to {UPSTREAM}...")
        upstream_ws = await websockets.connect(UPSTREAM, ping_interval=None)
        print("    Upstream connected. Sending subscription...")
        await upstream_ws.send(json.dumps(sub))
        print("    Streaming data to browser + writing to SQLite...")

        count = 0
        async for msg in upstream_ws:
            try:
                if isinstance(msg, bytes):
                    msg = msg.decode("utf-8")

                # ── NEW: persist before forwarding ────────────────────
                # Parsing failures never block the stream.
                try:
                    parsed = json.loads(msg)
                    STORE.record_aisstream(parsed)
                except Exception as exc:
                    print(f"    [store] skipped: {type(exc).__name__}: {exc}")

                await browser_ws.send(msg)
                count += 1
                if count <= 3:
                    print(f"    → msg #{count}: {msg[:150]}...")
                elif count % 500 == 0:
                    s = STORE.stats()
                    print(f"    → {count} forwarded | db: "
                          f"{s['positions']} pings, {s['vessels']} vessels")
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
    print("AIS WebSocket Proxy (with memory)")
    print(f"  Local:    ws://localhost:{LOCAL_PORT}")
    print(f"  Upstream: {UPSTREAM}")
    print(f"  DB:       {DB_PATH}")
    print( "  Area:     English Channel (48.3N-51.5N, 6W-2.5E)")
    print()
    print("Waiting for browser connections...")
    print("─" * 50)

    async with websockets.serve(proxy, "localhost", LOCAL_PORT):
        await asyncio.Future()  # run forever


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[stopped]")
    finally:
        STORE.close()
