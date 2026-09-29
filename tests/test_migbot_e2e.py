"""The real HTTP clients against a local mock of every API, in simulated time.

GeckoTerminal finds the migration, DexScreener prices it, a mock Solana node
answers the safety queries, RugCheck scores it, Jupiter quotes the fills and
GMGN answers with a Cloudflare challenge page (the bot must shrug that off).
"""
import base64
import hashlib
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import pytest

from migbot.config import SOL_MINT
from migbot.engine import Engine, Sources
from migbot.http import JsonHttp
from migbot.solana import BONDING_CURVE_DISCRIMINATOR, b58decode, b58encode, bonding_curve_address, find_program_address, is_on_curve
from migbot.sources.dexscreener import DexScreenerSource
from migbot.sources.geckoterminal import GeckoTerminalSource
from migbot.sources.gmgn import GmgnSource
from migbot.sources.jupiter import JupiterSource
from migbot.sources.rugcheck import RugcheckSource
from migbot.sources.solana_rpc import SolanaRpc
from migbot.storage import read_csv
from migbot_fakes import T0, Clock, settings

MINT = b58encode(hashlib.sha256(b"e2e-mint").digest())
POOL = find_program_address([b"pool", b58decode(MINT)], "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA")[0]


def wallet(i):
    n = 0
    while True:
        raw = hashlib.sha256(f"w{i}-{n}".encode()).digest()
        if is_on_curve(raw):
            return b58encode(raw)
        n += 1


WALLETS = [wallet(i) for i in range(12)]
CREATOR = wallet(99)


def price_at(s):
    """USD price by seconds since migration."""
    if s < 600:
        return 0.0001
    if s < 900:
        return 0.00025
    if s < 1200:
        return 0.0003
    return 0.0002


class MockApis(BaseHTTPRequestHandler):
    clock = None

    def log_message(self, *args):
        pass

    def _json(self, data, code=200):
        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        url = urlparse(self.path)
        s = self.clock() - T0
        if url.path == "/gecko/networks/solana/new_pools":
            pool = {
                "id": f"solana_{POOL}", "type": "pool",
                "attributes": {"address": POOL, "name": "E2E / SOL", "pool_created_at": "2026-09-21T14:13:20Z"},
                "relationships": {
                    "base_token": {"data": {"id": f"solana_{MINT}", "type": "token"}},
                    "quote_token": {"data": {"id": f"solana_{SOL_MINT}", "type": "token"}},
                    "dex": {"data": {"id": "pumpswap", "type": "dex"}},
                },
            }
            included = [{"id": f"solana_{MINT}", "type": "token", "attributes": {"symbol": "E2E", "name": "End to end", "decimals": 6}}]
            return self._json({"data": [pool], "included": included})
        if url.path.startswith("/dex/tokens/v1/solana/"):
            if s < 45:
                return self._json([])  # not indexed yet
            p = price_at(s)
            pair = {
                "chainId": "solana", "dexId": "pumpswap", "pairAddress": POOL, "url": f"https://dexscreener.com/solana/{POOL}",
                "baseToken": {"address": MINT, "symbol": "E2E", "name": "End to end"},
                "quoteToken": {"address": SOL_MINT, "symbol": "SOL"},
                "priceUsd": str(p), "priceNative": str(p / 150),
                "txns": {"m5": {"buys": 90, "sells": 50}}, "volume": {"m5": 12000}, "liquidity": {"usd": 30000},
                "marketCap": p * 1e9, "fdv": p * 1e9, "pairCreatedAt": int(T0 * 1000),
            }
            return self._json([pair])
        if url.path.startswith("/rug/tokens/"):
            return self._json({"score": 1, "score_normalised": 1, "risks": [{"name": "Mutable metadata", "level": "warn"}]})
        if url.path.startswith("/gmgn/"):
            body = b"<!DOCTYPE html><html><title>Just a moment...</title>cloudflare</html>"
            self.send_response(403)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return None
        if url.path == "/jup/quote":
            q = {k: v[0] for k, v in parse_qs(url.query).items()}
            native = price_at(s) / 150
            amount = int(q["amount"])
            if q["inputMint"] == SOL_MINT:
                out = int(amount / 1e9 / native * 0.99 * 1e6)
            else:
                out = int(amount / 1e6 * native * 0.99 * 1e9)
            return self._json({"inAmount": str(amount), "outAmount": str(out), "priceImpactPct": "0.004",
                               "routePlan": [{"swapInfo": {"label": "Pump.fun Amm"}, "percent": 100}]})
        return self._json({"error": "not found"}, 404)

    def do_POST(self):  # noqa: N802
        payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        method, params = payload["method"], payload["params"]
        result = {"context": {"slot": 1}}
        if method == "getAccountInfo" and params[0] == MINT:
            info = {"decimals": 6, "supply": str(10**15), "mintAuthority": None, "freezeAuthority": None}
            result["value"] = {"owner": "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
                               "data": {"program": "spl-token", "parsed": {"type": "mint", "info": info}}}
        elif method == "getAccountInfo" and params[0] == bonding_curve_address(MINT):
            raw = BONDING_CURVE_DISCRIMINATOR + bytes(40) + b"\x01" + b58decode(CREATOR)
            result["value"] = {"data": [base64.b64encode(raw).decode(), "base64"]}
        elif method == "getTokenLargestAccounts":
            rows = [{"address": "vault", "amount": str(500 * 10**12)}]
            rows += [{"address": f"acc{i}", "amount": str((20 - i) * 10**12)} for i in range(12)]
            result["value"] = rows
        elif method == "getMultipleAccounts":
            result["value"] = [
                {"data": {"parsed": {"info": {"owner": POOL if a == "vault" else WALLETS[int(a[3:])]}}}} for a in params[0]
            ]
        elif method == "getTokenAccountsByOwner":
            result["value"] = []  # the creator sold everything
        else:
            return self._json({"jsonrpc": "2.0", "id": payload["id"], "error": {"code": -32601, "message": "nope"}})
        return self._json({"jsonrpc": "2.0", "id": payload["id"], "result": result})


@pytest.fixture
def mock_server():
    clock = Clock()
    handler = type("H", (MockApis,), {"clock": staticmethod(clock)})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield clock, f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


def fast(name):
    return JsonHttp(name, min_interval=0, retries=1, timeout=5)


def test_full_lifecycle_through_real_clients(tmp_path, mock_server):
    clock, base = mock_server
    s = settings(tmp_path, discovery__mint_suffixes=[])
    src = Sources(
        gecko=GeckoTerminalSource(f"{base}/gecko", ["pumpswap"], [], http=fast("GeckoTerminal")),
        dexscreener=DexScreenerSource(f"{base}/dex", http=fast("DexScreener")),
        rpc=SolanaRpc(f"{base}/rpc", http=fast("Solana RPC")),
        rugcheck=RugcheckSource(f"{base}/rug", http=fast("RugCheck")),
        gmgn=GmgnSource(f"{base}/gmgn", http=fast("GMGN")),
        jupiter=JupiterSource(f"{base}/jup", http=fast("Jupiter")),
    )
    engine = Engine(s, src, clock=clock)
    clock.t = T0 + 20  # GeckoTerminal reports the pool 20 s after it was created
    end = T0 + 7400
    while clock() < end:
        engine.tick()
        clock.advance(5)

    trades = read_csv(os.path.join(s.data_dir, "trades.csv"))
    assert [(t["side"], t["reason"]) for t in trades] == [
        ("BUY", "lolos filter"), ("SELL", "take profit +100%"), ("SELL", "trailing stop"),
    ]
    assert all(t["method"] == "jupiter" for t in trades)
    assert float(trades[-1]["position_pnl_sol"]) > 0.1

    row = read_csv(os.path.join(s.data_dir, "tokens.csv"))[0]
    assert row["mint"] == MINT and row["symbol"] == "E2E" and row["status"] == "dibeli"
    assert row["source"] == "geckoterminal"
    assert float(row["top10_pct"]) == pytest.approx(sum(range(11, 21)) * 10**12 / 10**15 * 100)
    assert float(row["dev_pct"]) == 0.0 and row["rugcheck_score"] == "1"

    health = {h["name"]: h for h in src.health()}
    assert health["DexScreener"]["ok_count"] > 100 and health["Solana RPC"]["error_count"] == 0
    assert "diblokir" in health["GMGN"]["note"]
    assert health["GMGN"]["error_count"] <= 2  # paused after the first block, not hammered
