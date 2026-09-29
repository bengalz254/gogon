"""On-chain safety data over plain Solana JSON-RPC (any RPC URL works).

Per token: mint/freeze authority and Token-2022 extensions, the top holders
(pool vaults and burn addresses left out), and how much the creator (read
from pump.fun's bonding-curve account) still holds.
"""
from __future__ import annotations

import base64
import logging
import time

from migbot.http import HttpError, JsonHttp
from migbot.models import SafetyReport
from migbot.parsing import integer, num, sub, text
from migbot.solana import b58decode, bonding_curve_address, bonding_curve_creator, is_on_curve

logger = logging.getLogger("migbot.rpc")


class RpcError(RuntimeError):
    pass


def parse_mint_info(value) -> dict:
    """getAccountInfo(jsonParsed) of a mint -> decimals, supply, authorities, program, bad-extension candidates."""
    if not isinstance(value, dict):
        raise RpcError("mint tidak ditemukan")
    data = sub(value, "data")
    parsed = sub(data, "parsed")
    info = sub(parsed, "info")
    if parsed.get("type") != "mint" or "decimals" not in info:
        raise RpcError("akun ini bukan mint token")
    extensions = []
    for ext in info.get("extensions") or []:
        if not isinstance(ext, dict):
            continue
        name = text(ext, "extension")
        state = sub(ext, "state")
        if name == "transferFeeConfig":
            newer = num(sub(state, "newerTransferFee"), "transferFeeBasisPoints", default=0) or 0
            older = num(sub(state, "olderTransferFee"), "transferFeeBasisPoints", default=0) or 0
            if newer == 0 and older == 0 and not state.get("transferFeeConfigAuthority"):
                continue  # zero fee that nobody can raise
        if name:
            extensions.append(name)
    return {
        "decimals": integer(info, "decimals", default=0),
        "supply_raw": int(info.get("supply") or 0),
        "mint_authority": info.get("mintAuthority") or None,
        "freeze_authority": info.get("freezeAuthority") or None,
        "program": text(data, "program") or text(value, "owner"),
        "extensions": extensions,
    }


def parse_largest_accounts(value) -> list[tuple[str, int]]:
    out = []
    for row in value or []:
        if isinstance(row, dict) and text(row, "address"):
            try:
                out.append((text(row, "address"), int(row.get("amount") or 0)))
            except (TypeError, ValueError):
                continue
    return out


def parse_owners(addresses: list[str], value) -> dict[str, str]:
    owners = {}
    for address, account in zip(addresses, value or []):
        info = sub(sub(sub(account, "data"), "parsed"), "info")
        owner = text(info, "owner")
        if owner:
            owners[address] = owner
    return owners


def parse_owner_balance(value) -> int:
    total = 0
    for row in value or []:
        info = sub(sub(sub(sub(row, "account"), "data"), "parsed"), "info")
        try:
            total += int(sub(info, "tokenAmount").get("amount") or 0)
        except (TypeError, ValueError):
            continue
    return total


def is_program_owned(owner: str) -> bool:
    """Wallet addresses are ed25519 keys; program-derived addresses (pool
    vaults, lockers, bonding curves) are deliberately off the curve."""
    try:
        raw = b58decode(owner)
    except ValueError:
        return False
    return len(raw) == 32 and not is_on_curve(raw)


def concentration(
    largest: list[tuple[str, int]], owners: dict[str, str], supply_raw: int, excluded_owners: set[str]
) -> tuple[float | None, float | None, int, int]:
    """(top-10 %, top-holder %, wallets counted, accounts left out) of supply.

    Only accounts owned by ordinary wallets count as holders: pool vaults and
    other program-owned accounts, plus the listed addresses, are left out.
    """
    if supply_raw <= 0:
        return None, None, 0, 0
    kept, dropped = [], 0
    for address, amount in largest:
        if amount <= 0:
            continue
        owner = owners.get(address)
        if owner in excluded_owners or (owner and is_program_owned(owner)):
            dropped += 1
            continue
        kept.append(amount)
    kept.sort(reverse=True)
    top10 = sum(kept[:10]) / supply_raw * 100
    top1 = kept[0] / supply_raw * 100 if kept else 0.0
    return top10, top1, len(kept), dropped


class SolanaRpc:
    def __init__(self, url: str, http: JsonHttp | None = None):
        self.url = url
        self.http = http or JsonHttp("Solana RPC", min_interval=0.15, retries=3, timeout=15)
        self._id = 0

    def call(self, method: str, params: list):
        self._id += 1
        payload = self.http.post_json(self.url, {"jsonrpc": "2.0", "id": self._id, "method": method, "params": params})
        if not isinstance(payload, dict):
            raise RpcError(f"{method}: jawaban RPC tidak dikenal")
        if payload.get("error"):
            err = payload["error"]
            message = err.get("message") if isinstance(err, dict) else str(err)
            raise RpcError(f"{method}: {message}")
        result = payload.get("result")
        return result.get("value") if isinstance(result, dict) and "value" in result else result

    def mint_info(self, mint: str) -> dict:
        return parse_mint_info(self.call("getAccountInfo", [mint, {"encoding": "jsonParsed", "commitment": "confirmed"}]))

    def largest_accounts(self, mint: str) -> list[tuple[str, int]]:
        return parse_largest_accounts(self.call("getTokenLargestAccounts", [mint, {"commitment": "confirmed"}]))

    def owners(self, addresses: list[str]) -> dict[str, str]:
        if not addresses:
            return {}
        value = self.call("getMultipleAccounts", [addresses, {"encoding": "jsonParsed", "commitment": "confirmed"}])
        return parse_owners(addresses, value)

    def account_bytes(self, address: str) -> bytes | None:
        value = self.call("getAccountInfo", [address, {"encoding": "base64", "commitment": "confirmed"}])
        if not isinstance(value, dict):
            return None
        data = value.get("data")
        if isinstance(data, list) and data and isinstance(data[0], str):
            return base64.b64decode(data[0])
        return None

    def owner_balance(self, owner: str, mint: str) -> int:
        value = self.call(
            "getTokenAccountsByOwner", [owner, {"mint": mint}, {"encoding": "jsonParsed", "commitment": "confirmed"}]
        )
        return parse_owner_balance(value)

    def creator_of(self, mint: str) -> str | None:
        data = self.account_bytes(bonding_curve_address(mint))
        return bonding_curve_creator(data) if data else None

    def safety_report(self, mint: str, pair_address: str, amm_owners: list[str], creator: str | None = None) -> SafetyReport:
        """Everything the safety filters need. Steps that fail are listed in `errors`."""
        report = SafetyReport(ts=time.time())
        supply_raw = 0
        try:
            info = self.mint_info(mint)
            report.decimals = info["decimals"]
            supply_raw = info["supply_raw"]
            report.supply_ui = supply_raw / 10 ** info["decimals"]
            report.mint_authority = info["mint_authority"]
            report.freeze_authority = info["freeze_authority"]
            report.token_program = info["program"]
            report.extensions = info["extensions"]
            report.authorities_known = True
        except (RpcError, HttpError) as exc:
            report.errors.append(f"mint: {exc}")

        if supply_raw > 0:
            try:
                largest = self.largest_accounts(mint)
                owners = self.owners([a for a, _ in largest])
                excluded = set(amm_owners) | {pair_address, bonding_curve_address(mint)}
                top10, top1, kept, dropped = concentration(largest, owners, supply_raw, excluded)
                report.top10_pct, report.top_holder_pct = top10, top1
                report.holders_seen, report.excluded_accounts = kept, dropped
            except (RpcError, HttpError) as exc:
                report.errors.append(f"holder: {exc}")

            try:
                report.creator = creator or self.creator_of(mint)
                if report.creator:
                    report.dev_pct = self.owner_balance(report.creator, mint) / supply_raw * 100
            except (RpcError, HttpError) as exc:
                report.errors.append(f"dev: {exc}")
        return report
