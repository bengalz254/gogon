"""Up/Down engine: trades Polymarket's short-horizon crypto "Up or Down"
markets (e.g. "Bitcoin Up or Down - 5 minute") with a settlement-aware
fair-value model instead of direction guessing.

Layers (see README "Up/Down engine"):
  1. data      -- feeds/ (Chainlink via RTDS, CLOB order books, CEX trades),
                  discovery.py (Gamma API slugs -> token ids)
  2. state     -- window.py (per-window state machine), series.py, model.py
  3. strategy  -- strategies/ (fair value, late-certainty maker, constellation,
                  pair barbell, cheap asymmetric)
  4. risk/exec -- risk.py, orders.py (order manager + paper exchange),
                  broker_live.py, engine.py (the pure core), runner.py (asyncio shell)
"""
