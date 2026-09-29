"""Data sources. Each source parses its payloads with pure functions
(`parse_*`) so they can be tested offline, and records its health for the
dashboard. None of them needs a wallet or a private key.
"""
