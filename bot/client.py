"""Thin wrapper around Polymarket's CLOB V2 client (py-clob-client-v2).

Polymarket moved trading to CLOB V2 on April 28, 2026: new exchange
contracts, a new signed-order format and pUSD (Polymarket USD, backed 1:1 by
USDC) as collateral. Orders signed by the old py-clob-client are rejected,
so the bot uses the V2 SDK.

In paper mode we never construct a signing client at all — market data
(order books, prices) is public/read-only, so we use an unauthenticated
client for that and never touch order placement.
"""
from __future__ import annotations

import logging

from py_clob_client_v2 import ClobClient

from bot.config import WalletConfig

logger = logging.getLogger("polybot.client")


def build_client(wallet: WalletConfig) -> ClobClient:
    """Build a ClobClient.

    - Live trading: fully authenticated client with signing key + derived API creds.
    - Paper trading: read-only client (no key) used only for public market-data
      endpoints (order books, prices). No order-signing capability is exercised.
    """
    if wallet.live_trading:
        client = ClobClient(
            wallet.clob_host,
            chain_id=wallet.chain_id,
            key=wallet.private_key,
            signature_type=wallet.signature_type,
            funder=wallet.funder_address,
        )
        client.set_api_creds(client.create_or_derive_api_key())
        logger.info("Live ClobClient (CLOB V2) initialized with API credentials.")
        return client

    client = ClobClient(wallet.clob_host, chain_id=wallet.chain_id)
    logger.info("Read-only ClobClient (CLOB V2) initialized (paper trading mode).")
    return client
