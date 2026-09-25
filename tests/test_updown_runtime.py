"""The event loop must keep reading its websockets: a frozen console must not
block it, and when something else does, the watchdog says where."""
import io
import logging
import threading
import time

from bot.logger import setup_logging
from bot.updown.runner import LoopWatchdog


class _FrozenConsole(io.StringIO):
    """A Windows console with text selected in it: writes block until released."""

    def __init__(self):
        super().__init__()
        self.released = threading.Event()

    def write(self, s):
        self.released.wait(5)
        return super().write(s)


def test_a_frozen_console_does_not_block_logging(tmp_path):
    polybot, root = logging.getLogger("polybot"), logging.getLogger()
    saved = (polybot.handlers[:], polybot.propagate, polybot.level, root.handlers[:])
    polybot.handlers.clear()
    console = _FrozenConsole()
    try:
        log = setup_logging(str(tmp_path), console_stream=console)
        t0 = time.perf_counter()
        log.info("window listed")
        assert time.perf_counter() - t0 < 0.5  # returned while the console is still frozen
        assert "window listed" in (tmp_path / "bot.log").read_text(encoding="utf-8")
        console.released.set()
        deadline = time.time() + 3
        while "window listed" not in console.getvalue() and time.time() < deadline:
            time.sleep(0.02)
        assert "INFO     polybot: window listed" in console.getvalue()
    finally:
        console.released.set()
        for h in polybot.handlers:
            if getattr(h, "listener", None) is not None:
                h.listener.stop()
            h.close()
        polybot.handlers[:], polybot.propagate, polybot.level = saved[0], saved[1], saved[2]
        root.handlers[:] = saved[3]
        logging.captureWarnings(False)


def test_watchdog_reports_where_the_loop_is_stuck(caplog):
    wd = LoopWatchdog(threshold_s=0.2, poll_s=0.02)

    def stuck_writing_to_the_console():
        wd.tick()
        time.sleep(0.6)  # the loop's thread (this one) is blocked

    with caplog.at_level("WARNING", logger="polybot.updown.runner"):
        wd.start()
        try:
            stuck_writing_to_the_console()
            wd.tick()  # the loop runs again
            deadline = time.time() + 2
            while not any("running again" in r.getMessage() for r in caplog.records) and time.time() < deadline:
                time.sleep(0.02)
        finally:
            wd.stop()
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "stuck here" in text and "stuck_writing_to_the_console" in text
    assert "running again after a 0." in text


def test_region_check_reads_polymarkets_answer(monkeypatch):
    import requests

    from bot.updown import runner as runner_mod

    class _Resp:
        def __init__(self, data):
            self.data = data

        def json(self):
            return self.data

    monkeypatch.setattr(requests, "get", lambda url, timeout: _Resp({"blocked": True, "country": "GB", "region": "ENG"}))
    assert runner_mod.region_check() == {"blocked": True, "country": "GB", "region": "ENG"}
    monkeypatch.setattr(requests, "get", lambda url, timeout: _Resp({"error": "unexpected"}))
    assert runner_mod.region_check() is None

    def offline(url, timeout):
        raise requests.ConnectionError("offline")

    monkeypatch.setattr(requests, "get", offline)
    assert runner_mod.region_check() is None


def test_live_mode_refuses_to_start_where_polymarket_blocks_trading(tmp_path, monkeypatch):
    import asyncio

    from bot.config import WalletConfig
    from bot.updown import runner as runner_mod
    from bot.updown.config import UpDownConfig

    monkeypatch.setattr(runner_mod, "region_check", lambda: {"blocked": True, "country": "GB", "region": "ENG"})
    cfg = UpDownConfig()
    cfg.journal.dir = str(tmp_path)
    cfg.journal.trades_csv = str(tmp_path / "trades.csv")
    live = runner_mod.Runner(cfg, WalletConfig("0x" + "1" * 64, 137, "http://127.0.0.1:1", 0, None, True))
    try:
        assert asyncio.run(live.run()) == runner_mod.EXIT_REFUSED
    finally:
        live.journal.close()
