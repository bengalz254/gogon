"""migbot: watches pump.fun meme coins that have just migrated to an AMM pool.

It finds each migration, watches the token for a few minutes, runs GMGN-style
checks (liquidity, volume, buy pressure, holder concentration, dev holdings,
mint/freeze authority, RugCheck, optional GMGN data) and paper trades the
tokens that pass. Every migrated token's price path is recorded, so the
report can show whether the filters pick better tokens than chance.

Paper mode only: no wallet or private key is used anywhere.
"""

__version__ = "1.0.0"
