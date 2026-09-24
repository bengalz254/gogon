"""Radar dashboard for the 5-minute Up/Down bot.

Run it next to the bot (second terminal, venv active):

    python scripts/updown_dashboard.py          # watch the running bot
    python scripts/updown_dashboard.py --demo   # no bot needed: runs the simulator

Then open http://127.0.0.1:8766 (it opens automatically). The bot writes
data/updown_state.json every second; this server hands that file to the
page. Nothing leaves your machine and no internet is needed.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

HTML_PATH = os.path.join(_ROOT, "scripts", "updown_dashboard.html")
STALE_AFTER_S = 5.0


class StateSource:
    """Where /api/state comes from: the bot's state file, or the in-process demo."""

    def __init__(self, state_path: str):
        self.state_path = state_path
        self.demo_state: dict | None = None
        self.demo = False

    def payload(self) -> dict:
        if self.demo:
            if self.demo_state is None:
                return {"status": "starting"}
            return {"status": "online", "demo": True, "state": self.demo_state}
        try:
            age = time.time() - os.path.getmtime(self.state_path)
            with open(self.state_path, encoding="utf-8") as f:
                state = json.load(f)
        except FileNotFoundError:
            return {"status": "no_bot", "hint": f"{self.state_path} not found. Start the bot: python -m updown.main"}
        except (OSError, ValueError):
            return {"status": "retry"}  # mid-write on Windows; the page keeps the last frame
        return {"status": "online" if age < STALE_AFTER_S else "stale", "age": age, "state": state}


def run_demo(source: StateSource, speed: float, warmup_windows: int, seed: int) -> None:
    """Drive the real engine against the simulator, in (sped-up) real time."""
    from updown.broker import PaperBroker
    from updown.config import load_updown_settings
    from updown.engine import UpDownEngine
    from updown.feeds import PriceHistory
    from updown.markets import window_start
    from updown.sim import SimGateway, SimWorld
    from updown.state import build_state

    logging.getLogger("polybot").setLevel(logging.WARNING)
    settings = load_updown_settings(os.path.join(_ROOT, "config", "updown.yaml"))
    settings.assets = ["btc"]
    world = SimWorld(seed=seed, lag_s=1.5, mm_noise=0.01)
    history = PriceHistory(maxlen=4000)
    engine = UpDownEngine(settings, SimGateway(world), PaperBroker(), history, journal=None)
    engine.vol["btc"].seed(world.sigma)

    def step(t: int) -> None:
        price = world.step(t)
        history.add("btc", float(t), price)
        engine.on_price("btc", float(t), price)
        engine.tick(float(t))

    # Fast-forward some history so the page isn't empty, then go real time,
    # landing a little way into a fresh window.
    now = int(time.time())
    t = window_start(now, 300) - warmup_windows * 300 - 5
    target = window_start(now, 300) + 20
    while t < target:
        step(t)
        t += 1
    source.demo_state = build_state(engine, float(t))
    while True:
        time.sleep(1.0 / speed)
        step(t)
        source.demo_state = build_state(engine, float(t))
        t += 1


def make_handler(source: StateSource):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):  # keep the console quiet
            pass

        def _send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            path = self.path.split("?", 1)[0]
            if path == "/api/state":
                self._send(200, json.dumps(source.payload()).encode("utf-8"), "application/json")
            elif path in ("/", "/index.html"):
                with open(HTML_PATH, "rb") as f:
                    self._send(200, f.read(), "text/html; charset=utf-8")
            else:
                self._send(404, b"not found", "text/plain")

    return Handler


def main() -> None:
    ap = argparse.ArgumentParser(description="Radar dashboard for the 5-minute Up/Down bot")
    ap.add_argument("--port", type=int, default=int(os.environ.get("UPDOWN_DASHBOARD_PORT", "8766")))
    ap.add_argument("--state", default=os.path.join(_ROOT, "data", "updown_state.json"))
    ap.add_argument("--demo", action="store_true", help="run the simulator instead of watching the bot")
    ap.add_argument("--speed", type=float, default=1.0, help="demo: simulated seconds per real second")
    ap.add_argument("--warmup", type=int, default=24, help="demo: windows of history to pre-simulate")
    ap.add_argument("--seed", type=int, default=11)
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args()

    source = StateSource(args.state)
    if args.demo:
        source.demo = True
        threading.Thread(target=run_demo, args=(source, args.speed, args.warmup, args.seed), daemon=True).start()

    server = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(source))
    url = f"http://127.0.0.1:{args.port}"
    print(f"Radar dashboard: {url}  ({'DEMO / simulator' if args.demo else 'watching ' + args.state})")
    print("Ctrl+C to stop.")
    if not args.no_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
