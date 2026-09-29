"""Payload parsing for every data source, offline (fixtures follow the public API shapes)."""
import base64
import hashlib
import json

import pytest

from migbot.config import SOL_MINT
from migbot.http import Health, HttpError
from migbot.models import MarketSnapshot
from migbot.solana import BONDING_CURVE_DISCRIMINATOR, b58decode, b58encode, bonding_curve_address, find_program_address, is_on_curve
from migbot.sources.dexscreener import DexScreenerSource, best_pair, parse_pairs, snapshot_from_pair
from migbot.sources.geckoterminal import parse_new_pools
from migbot.sources.gmgn import GmgnSource, parse_token_info
from migbot.sources.jupiter import NoRoute, parse_quote
from migbot.sources.pumpportal import PumpPortalFeed, parse_migration
from migbot.sources.rugcheck import parse_summary
from migbot.sources.solana_rpc import SolanaRpc, concentration, is_program_owned, parse_mint_info

MINT = b58encode(hashlib.sha256(b"mint").digest())[:-4] + "pump"
while len(b58decode(MINT)) != 32:  # make sure the fixture mint is a real 32-byte address
    MINT = b58encode(hashlib.sha256(MINT.encode()).digest())[:-4] + "pump"


def wallet(seed: str) -> str:
    """An on-curve (wallet-like) address."""
    i = 0
    while True:
        raw = hashlib.sha256(f"{seed}-{i}".encode()).digest()
        if is_on_curve(raw):
            return b58encode(raw)
        i += 1


def pda(seed: str) -> str:
    return find_program_address([seed.encode()], "11111111111111111111111111111111")[0]


# ---------------------------------------------------------------- PumpPortal
def test_pumpportal_migration_event():
    ev = parse_migration({"signature": "5sig", "mint": MINT, "txType": "migrate", "pool": "pump-amm"}, 1000.0)
    assert ev.mint == MINT and ev.pool == "pump-amm" and ev.signature == "5sig"
    assert ev.migrated_at == 1000.0 and ev.source == "pumpportal"


def test_pumpportal_ignores_other_messages():
    assert parse_migration({"message": "Successfully subscribed to keys."}, 1.0) is None
    assert parse_migration({"mint": MINT, "txType": "buy"}, 1.0) is None
    assert parse_migration({"mint": "not-a-mint"}, 1.0) is None
    assert parse_migration(["x"], 1.0) is None


def test_pumpportal_event_timestamp_used_only_when_plausible():
    now = 1_790_000_000.0
    assert parse_migration({"mint": MINT, "timestamp": 1_789_999_990_000}, now).migrated_at == now - 10  # ms
    assert parse_migration({"mint": MINT, "timestamp": 1_789_999_995}, now).migrated_at == now - 5  # s
    assert parse_migration({"mint": MINT, "timestamp": 1_700_000_000}, now).migrated_at == now  # implausible


def test_pumpportal_feed_queues_events():
    feed = PumpPortalFeed("wss://example")
    assert feed.handle_message(json.dumps({"message": "Successfully subscribed"})) is None
    assert feed.handle_message("not json") is None
    feed.handle_message(json.dumps({"mint": MINT, "txType": "migrate"}))
    events = feed.drain()
    assert [e.mint for e in events] == [MINT] and feed.event_count == 1 and feed.drain() == []
    assert feed.snapshot()["event_count"] == 1


# ---------------------------------------------------------------- GeckoTerminal
def gecko_pool(base, quote, dex="pumpswap", created="2026-09-29T06:00:00Z"):
    return {
        "id": "solana_POOL" + base[:4],
        "type": "pool",
        "attributes": {"address": "POOL" + base[:4], "name": "TEST / SOL", "pool_created_at": created},
        "relationships": {
            "base_token": {"data": {"id": f"solana_{base}", "type": "token"}},
            "quote_token": {"data": {"id": f"solana_{quote}", "type": "token"}},
            "dex": {"data": {"id": dex, "type": "dex"}},
        },
    }


def test_gecko_new_pools_keeps_pumpswap_sol_pairs_of_pump_mints():
    other = MINT[:-4] + "bonk"
    payload = {
        "data": [
            gecko_pool(MINT, SOL_MINT),
            gecko_pool(SOL_MINT, MINT),  # listed the other way round
            gecko_pool(MINT, "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"),  # USDC pair
            gecko_pool(MINT, SOL_MINT, dex="raydium"),
            gecko_pool(other, SOL_MINT),  # not a pump.fun mint
        ],
        "included": [{"id": f"solana_{MINT}", "type": "token", "attributes": {"symbol": "TST", "name": "Test", "decimals": 6}}],
    }
    events = parse_new_pools(payload, ["pumpswap"], ["pump"], received_at=1e10)
    assert [e.mint for e in events] == [MINT, MINT]
    first = events[0]
    assert first.symbol == "TST" and first.decimals == 6 and first.dex == "pumpswap"
    assert first.migrated_at == pytest.approx(1790661600.0)  # 2026-09-29T06:00:00Z
    assert parse_new_pools({"data": "bad"}, ["pumpswap"], ["pump"], 1.0) == []
    assert parse_new_pools(None, ["pumpswap"], ["pump"], 1.0) == []


# ---------------------------------------------------------------- DexScreener
def ds_pair(pair, base, quote=SOL_MINT, liq=20000.0, **extra):
    raw = {
        "chainId": "solana",
        "dexId": "pumpswap",
        "url": f"https://dexscreener.com/solana/{pair}",
        "pairAddress": pair,
        "baseToken": {"address": base, "name": "Test", "symbol": "TST"},
        "quoteToken": {"address": quote, "name": "Wrapped SOL", "symbol": "SOL"},
        "priceNative": "0.0000006",
        "priceUsd": "0.00009",
        "txns": {"m5": {"buys": 70, "sells": 30}, "h1": {"buys": 400, "sells": 200}},
        "volume": {"m5": 8000.5, "h1": 50000},
        "priceChange": {"m5": 3.2, "h1": -10},
        "liquidity": {"usd": liq, "base": 1e8, "quote": 60.0},
        "fdv": 90000,
        "marketCap": 90000,
        "pairCreatedAt": 1790661600000,
        "info": {
            "websites": [{"label": "Website", "url": "https://test.example"}],
            "socials": [{"type": "twitter", "url": "https://x.com/test"}, {"platform": "telegram", "handle": "testtg"}],
        },
    }
    raw.update(extra)
    return raw


def test_dexscreener_snapshot_fields():
    snap = snapshot_from_pair(ds_pair("PAIR1", MINT), 123.0)
    assert snap.price_usd == 0.00009 and snap.price_native == 0.0000006
    assert snap.liquidity_usd == 20000.0 and snap.market_cap_usd == 90000
    assert snap.txns_m5 == 100 and snap.buy_ratio_m5 == 0.7
    assert snap.pair_created_at == 1790661600.0
    assert snap.socials == ["https://test.example", "https://x.com/test", "telegram:testtg"]
    assert MarketSnapshot.from_dict(snap.to_dict()) == snap


def test_dexscreener_pair_choice():
    pairs = [ds_pair("SMALL", MINT, liq=5000), ds_pair("BIG", MINT, liq=50000), ds_pair("USDC", MINT, quote="USDCmint", liq=1e6)]
    assert best_pair(pairs, MINT)["pairAddress"] == "BIG"
    assert best_pair(pairs, MINT, known_pair="SMALL")["pairAddress"] == "SMALL"
    assert best_pair(pairs, "other") is None
    assert parse_pairs({"schemaVersion": "1", "pairs": pairs}) == pairs  # older endpoint shape
    assert parse_pairs(None) == []


def test_dexscreener_missing_fields_become_none():
    snap = snapshot_from_pair({"pairAddress": "P", "priceUsd": "abc", "txns": {"m5": {"buys": 3}}}, 1.0)
    assert snap.price_usd is None and snap.liquidity_usd is None and snap.txns_m5 is None and snap.buy_ratio_m5 is None


class FakeHttp:
    def __init__(self, responses=None, error=None):
        self.responses = list(responses or [])
        self.error = error
        self.health = Health("fake")
        self.calls = []

    def get_json(self, url, params=None, headers=None):
        self.calls.append((url, params))
        if self.error:
            raise self.error
        return self.responses.pop(0)

    def post_json(self, url, payload, headers=None):
        self.calls.append((url, payload))
        if self.error:
            raise self.error
        return self.responses.pop(0)


def test_dexscreener_batches_of_30():
    mints = [f"{i:02d}" + MINT[2:] for i in range(35)]
    http = FakeHttp([[ds_pair("P0", mints[0])], [ds_pair("P34", mints[34])]])
    out = DexScreenerSource("https://api.dexscreener.com", http=http).fetch(mints)
    assert set(out) == {mints[0], mints[34]}
    assert len(http.calls) == 2 and http.calls[0][0].count(",") == 29


def test_dexscreener_raises_when_every_batch_fails():
    http = FakeHttp(error=HttpError("HTTP 500", 500))
    with pytest.raises(HttpError):
        DexScreenerSource("https://x", http=http).fetch([MINT])


# ---------------------------------------------------------------- RugCheck / GMGN / Jupiter
def test_rugcheck_summary():
    rep = parse_summary(
        {
            "tokenProgram": "Tokenkeg",
            "risks": [
                {"name": "Mutable metadata", "value": "", "description": "can change", "score": 100, "level": "warn"},
                {"name": "Freeze Authority still enabled", "description": "x", "score": 7500, "level": "danger"},
                {"bad": "row"},
            ],
            "score": 7601,
            "score_normalised": 61,
        }
    )
    assert rep.score_normalised == 61 and [r["level"] for r in rep.risks] == ["warn", "danger"]
    with pytest.raises(ValueError):
        parse_summary("nope")


def test_gmgn_token_info():
    info = parse_token_info(
        {"code": 0, "msg": "success", "data": {"token": {"holder_count": 812, "top_10_holder_rate": 0.183, "smart_buy_24h": 3,
                                                         "smart_sell_24h": 1, "sniper_count": 7, "creator_token_status": "creator_close"}}}
    )
    assert info.holders == 812 and info.top10_pct == pytest.approx(18.3) and info.smart_buys == 3
    assert info.sniper_count == 7 and info.dev_status == "creator_close"
    with pytest.raises(ValueError):
        parse_token_info({"code": 40001, "msg": "rate limited"})
    with pytest.raises(ValueError):
        parse_token_info({"code": 0, "data": {}})


def test_gmgn_block_pauses_requests():
    src = GmgnSource("https://gmgn.ai", blocked_retry_minutes=30, http=FakeHttp(error=HttpError("HTTP 403", 403, blocked=True)))
    assert src.available
    with pytest.raises(HttpError):
        src.fetch(MINT)
    assert not src.available and "diblokir" in src.http.health.note


def test_jupiter_quote():
    q = parse_quote(
        {"inAmount": "100000000", "outAmount": "123456789", "priceImpactPct": "0.0123",
         "routePlan": [{"swapInfo": {"label": "Pump.fun Amm"}, "percent": 100}]}
    )
    assert q.out_amount == 123456789 and q.price_impact_pct == pytest.approx(1.23) and q.route == "Pump.fun Amm"
    for bad in ({"error": "Could not find any route", "errorCode": "COULD_NOT_FIND_ANY_ROUTE"}, {"inAmount": "1", "outAmount": "0"}, {}):
        with pytest.raises(NoRoute):
            parse_quote(bad)


# ---------------------------------------------------------------- Solana RPC
def test_mint_info_and_extensions():
    value = {
        "owner": "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb",
        "data": {
            "program": "spl-token-2022",
            "parsed": {
                "type": "mint",
                "info": {
                    "decimals": 6,
                    "supply": "1000000000000000",
                    "mintAuthority": None,
                    "freezeAuthority": None,
                    "extensions": [
                        {"extension": "transferFeeConfig", "state": {"transferFeeConfigAuthority": None,
                                                                      "newerTransferFee": {"transferFeeBasisPoints": 0},
                                                                      "olderTransferFee": {"transferFeeBasisPoints": 0}}},
                        {"extension": "metadataPointer", "state": {}},
                        {"extension": "permanentDelegate", "state": {"delegate": "X"}},
                    ],
                },
            },
        },
    }
    info = parse_mint_info(value)
    assert info["decimals"] == 6 and info["supply_raw"] == 10**15
    assert info["extensions"] == ["metadataPointer", "permanentDelegate"]  # zero, frozen fee is harmless
    value["data"]["parsed"]["info"]["extensions"][0]["state"]["newerTransferFee"]["transferFeeBasisPoints"] = 500
    assert "transferFeeConfig" in parse_mint_info(value)["extensions"]


def test_concentration_leaves_out_pools_and_program_accounts():
    supply = 1_000_000
    w1, w2, pool_vault_owner, listed = wallet("a"), wallet("b"), pda("pool"), wallet("listed")
    largest = [("acc_pool", 600_000), ("acc_w1", 80_000), ("acc_listed", 50_000), ("acc_w2", 20_000), ("acc_zero", 0)]
    owners = {"acc_pool": pool_vault_owner, "acc_w1": w1, "acc_listed": listed, "acc_w2": w2}
    top10, top1, kept, dropped = concentration(largest, owners, supply, {listed})
    assert top10 == pytest.approx(10.0) and top1 == pytest.approx(8.0)
    assert kept == 2 and dropped == 2
    assert is_program_owned(pool_vault_owner) and not is_program_owned(w1)
    assert concentration(largest, owners, 0, set()) == (None, None, 0, 0)


class RpcRouter:
    """Answers JSON-RPC calls like a node would, for one mint."""

    def __init__(self, mint, pair, creator, dev_raw=20_000_000):
        self.health = Health("rpc")
        self.mint, self.pair, self.creator, self.dev_raw = mint, pair, creator, dev_raw
        self.curve = bonding_curve_address(mint)
        self.w = [wallet(f"h{i}") for i in range(12)]
        self.pool_owner = pair  # PumpSwap: the pool account owns its vaults

    def post_json(self, url, payload, headers=None):
        method, params = payload["method"], payload["params"]
        ctx = {"context": {"slot": 1}}
        if method == "getAccountInfo" and params[0] == self.mint:
            info = {"decimals": 6, "supply": str(10**15), "mintAuthority": None, "freezeAuthority": None}
            value = {"owner": "Tokenkeg", "data": {"program": "spl-token", "parsed": {"type": "mint", "info": info}}}
        elif method == "getAccountInfo" and params[0] == self.curve:
            data = BONDING_CURVE_DISCRIMINATOR + bytes(40) + b"\x01" + b58decode(self.creator)
            value = {"data": [base64.b64encode(data).decode(), "base64"]}
        elif method == "getTokenLargestAccounts":
            rows = [{"address": "vault", "amount": str(400 * 10**12)}]
            rows += [{"address": f"acc{i}", "amount": str((30 - i) * 10**12)} for i in range(12)]
            value = rows
        elif method == "getMultipleAccounts":
            value = []
            for addr in params[0]:
                owner = self.pool_owner if addr == "vault" else self.w[int(addr[3:])]
                value.append({"data": {"parsed": {"info": {"owner": owner}}}})
        elif method == "getTokenAccountsByOwner":
            assert params[0] == self.creator and params[1] == {"mint": self.mint}
            value = [{"account": {"data": {"parsed": {"info": {"tokenAmount": {"amount": str(self.dev_raw)}}}}}}]
        else:
            return {"jsonrpc": "2.0", "id": payload["id"], "error": {"code": -32601, "message": "Method not found"}}
        return {"jsonrpc": "2.0", "id": payload["id"], "result": {**ctx, "value": value}}


def test_safety_report_end_to_end():
    creator = wallet("creator")
    rpc = SolanaRpc("http://rpc", http=RpcRouter(MINT, pair=pda("pair"), creator=creator, dev_raw=10**13))
    rep = rpc.safety_report(MINT, rpc.http.pair, amm_owners=[])
    assert rep.errors == []
    assert rep.authorities_known and rep.mint_authority is None and rep.decimals == 6
    assert rep.creator == creator
    assert rep.dev_pct == pytest.approx(1.0)  # 10^13 of 10^15
    # top 10 of the 12 wallets (30..19 * 10^12), the pool vault left out
    assert rep.top10_pct == pytest.approx(sum(range(21, 31)) * 10**12 / 10**15 * 100)
    assert rep.top_holder_pct == pytest.approx(3.0)
    assert rep.excluded_accounts == 1


def test_safety_report_collects_errors_instead_of_raising():
    class Broken:
        health = Health("rpc")

        def post_json(self, url, payload, headers=None):
            raise HttpError("HTTP 429", 429)

    rep = SolanaRpc("http://rpc", http=Broken()).safety_report(MINT, "pair", [])
    assert not rep.authorities_known and rep.errors and rep.top10_pct is None
