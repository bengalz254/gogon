"""On-chain safety data over plain Solana JSON-RPC (any RPC URL works).

Per token: mint/freeze authority and Token-2022 extensions, the top holders
(pool vaults and burn addresses left out), how much the creator (read from
pump.fun's bonding-curve account) still holds, and optionally the number of
holders. Counting holders uses the DAS method getTokenAccounts (Helius and
other DAS providers); on an RPC without it the count is simply left out.
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


HOLDER_PAGE_SIZE = 1000  # getTokenAccounts maximum per page
HOLDER_MAX_PAGES = 2  # counts up to 2000 holders; more is reported as "2000+"


class RpcError(RuntimeError):
    def __init__(self, message: str, code: int | None = None):
        super().__init__(message)
        self.code = code

    @property
    def unsupported(self) -> bool:
        """The RPC does not offer this method at all (as opposed to a passing failure)."""
        text_ = str(self).lower()
        return self.code == -32601 or "method not found" in text_ or "not supported" in text_


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


def parse_token_accounts(result) -> list[tuple[str, int]]:
    """getTokenAccounts (DAS) page -> [(owner, raw amount)]."""
    rows = result.get("token_accounts") if isinstance(result, dict) else None
    out = []
    for row in rows or []:
        owner = text(row, "owner") if isinstance(row, dict) else ""
        if not owner:
            continue
        try:
            amount = int(row.get("amount") or 0)
        except (TypeError, ValueError):
            continue
        out.append((owner, amount))
    return out


def count_holders(rows: list[tuple[str, int]], excluded_owners: set[str]) -> int:
    """Distinct owners with a balance; the pool and other listed owners are left out."""
    return len({owner for owner, amount in rows if amount > 0 and owner not in excluded_owners})


def holder_distribution(rows: list[tuple[str, int]], excluded_owners: set[str], supply_raw: int) -> tuple[float | None, int | None]:
    """(% of supply held by the 50 largest wallets, number of wallets holding ≥ 1% of supply).

    Supply spread over many mid-size wallets is what a bundled launch looks like: the
    top-10 check misses it, these two numbers do not.
    """
    if supply_raw <= 0:
        return None, None
    per_owner: dict[str, int] = {}
    for owner, amount in rows:
        if amount > 0 and owner not in excluded_owners:
            per_owner[owner] = per_owner.get(owner, 0) + amount
    ranked = sorted(per_owner.items(), key=lambda kv: kv[1], reverse=True)[:60]
    sizes = [amount for owner, amount in ranked if not is_program_owned(owner)][:50]
    return sum(sizes) / supply_raw * 100, sum(1 for amount in sizes if amount * 100 >= supply_raw)


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
        self.holders_supported: bool | None = None  # False once the RPC turned out to lack getTokenAccounts

    def call(self, method: str, params: list | dict):
        self._id += 1
        payload = self.http.post_json(self.url, {"jsonrpc": "2.0", "id": self._id, "method": method, "params": params})
        if not isinstance(payload, dict):
            raise RpcError(f"{method}: jawaban RPC tidak dikenal")
        if payload.get("error"):
            err = payload["error"]
            message = err.get("message") if isinstance(err, dict) else str(err)
            code = err.get("code") if isinstance(err, dict) else None
            raise RpcError(f"{method}: {message}", code if isinstance(code, int) else None)
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

    def holder_count(self, mint: str, excluded_owners: set[str]) -> tuple[int, bool]:
        """(holders, complete). complete is False when there were more pages than were read."""
        rows, complete = self.holder_rows(mint)
        return count_holders(rows, excluded_owners), complete

    def holder_stats(self, mint: str, excluded_owners: set[str], supply_raw: int) -> dict:
        """Holder count, plus the distribution when every account was read."""
        rows, complete = self.holder_rows(mint)
        top50, big = holder_distribution(rows, excluded_owners, supply_raw) if complete else (None, None)
        return {
            "holder_count": count_holders(rows, excluded_owners),
            "holder_count_complete": complete,
            "top50_pct": top50,
            "wallets_1pct": big,
        }

    def holder_rows(self, mint: str) -> tuple[list[tuple[str, int]], bool]:
        """Every token account as (owner, raw amount), up to HOLDER_MAX_PAGES pages, and whether that was all."""
        rows: list[tuple[str, int]] = []
        for page in range(1, HOLDER_MAX_PAGES + 1):
            try:
                result = self.call("getTokenAccounts", {"mint": mint, "page": page, "limit": HOLDER_PAGE_SIZE})
            except RpcError as exc:
                if exc.unsupported:
                    self.holders_supported = False
                raise
            self.holders_supported = True
            if not isinstance(result, dict):
                raise RpcError("getTokenAccounts: jawaban RPC tidak dikenal")
            batch = result.get("token_accounts") or []
            rows.extend(parse_token_accounts(result))
            if len(batch) < HOLDER_PAGE_SIZE:
                return rows, True
        return rows, False

    def creator_of(self, mint: str) -> str | None:
        data = self.account_bytes(bonding_curve_address(mint))
        return bonding_curve_creator(data) if data else None

    def safety_report(
        self, mint: str, pair_address: str, amm_owners: list[str], creator: str | None = None, count_holders: bool = False
    ) -> SafetyReport:
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

        excluded = set(amm_owners) | {pair_address, bonding_curve_address(mint)}
        if supply_raw > 0:
            try:
                largest = self.largest_accounts(mint)
                owners = self.owners([a for a, _ in largest])
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

        if count_holders and supply_raw > 0 and self.holders_supported is not False:
            try:
                for key, value in self.holder_stats(mint, excluded, supply_raw).items():
                    setattr(report, key, value)
            except (RpcError, HttpError) as exc:
                if self.holders_supported is not False:  # an RPC without the method is reported once, by the engine
                    report.errors.append(f"jumlah holder: {exc}")
        return report
