"""Probe Polymarket's real-time data socket (RTDS) for the Chainlink TWAP feed.

Settlement of the 5-minute markets uses Chainlink's 60-second TWAP, and
Polymarket relays it on wss://ws-live-data.polymarket.com. Public write-ups
disagree on the exact subscribe message, so this tries the known variants
and prints what comes back. Run it on the server and paste the output:

    venv/bin/python scripts/probe_rtds.py

Read-only: it only listens. Needs `websocket-client` (in requirements.txt).
"""
from __future__ import annotations

import json
import sys
import threading
import time

try:
    import websocket  # websocket-client
except ImportError:
    sys.exit("Missing package: run  venv/bin/pip install websocket-client  and try again.")

URL = "wss://ws-live-data.polymarket.com"
LISTEN_S = 12

ATTEMPTS = {
    "A topic+filters (twap)": {
        "action": "subscribe",
        "subscriptions": [{"topic": "crypto_prices_chainlink_twap", "type": "*", "filters": json.dumps({"symbol": "btc/usd"})}],
    },
    "B topic, no filter (twap)": {
        "action": "subscribe",
        "subscriptions": [{"topic": "crypto_prices_chainlink_twap", "type": "*"}],
    },
    "C topic+filters (spot chainlink)": {
        "action": "subscribe",
        "subscriptions": [{"topic": "crypto_prices_chainlink", "type": "*", "filters": json.dumps({"symbol": "btc/usd"})}],
    },
    "D op/channel style (twap)": {
        "op": "subscribe",
        "rid": "probe",
        "subscriptions": [{"channel": "crypto_prices_chainlink_twap", "filter": {"symbol": "btc/usd"}}],
    },
}


def probe(name: str, msg: dict) -> None:
    print(f"\n=== {name} ===")
    print(f"send: {json.dumps(msg)}")
    got = []
    try:
        ws = websocket.create_connection(URL, timeout=5)
    except Exception as exc:
        print(f"CONNECT FAILED: {exc}")
        return
    stop = threading.Event()

    def ping():
        while not stop.wait(8):
            try:
                ws.send("PING")
            except Exception:
                return

    threading.Thread(target=ping, daemon=True).start()
    ws.send(json.dumps(msg))
    end = time.time() + LISTEN_S
    while time.time() < end and len(got) < 6:
        try:
            ws.settimeout(max(0.5, end - time.time()))
            raw = ws.recv()
        except websocket.WebSocketTimeoutException:
            break
        except Exception as exc:
            print(f"recv error: {exc}")
            break
        if raw in ("PONG", ""):
            continue
        got.append(raw)
        print(f"recv[{len(got)}]: {str(raw)[:600]}")
    stop.set()
    ws.close()
    if not got:
        print("(nothing received)")


if __name__ == "__main__":
    for name, msg in ATTEMPTS.items():
        probe(name, msg)
    print("\nDone. Paste everything above into the chat.")
