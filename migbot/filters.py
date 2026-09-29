"""GMGN-style entry filters. Pure functions: data in, a list of checks out.

Each check ends as "ok", "gagal" (failed) or "lewati" (skipped because its
data is unavailable; with filters.strict_missing_data a missing value fails
instead). GMGN-only checks are skipped whenever GMGN data is unavailable.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from migbot.config import FilterConfig, RugcheckConfig
from migbot.models import GmgnInfo, MarketSnapshot, RugcheckReport, SafetyReport

OK, FAIL, SKIP = "ok", "gagal", "lewati"


def fmt_usd(value: float | None) -> str:
    if value is None:
        return "?"
    sign = "-" if value < 0 else ""
    value = abs(value)
    if value >= 1_000_000:
        return f"{sign}${value / 1_000_000:.2f}M"
    if value >= 1_000:
        return f"{sign}${value / 1_000:.1f}k"
    return f"{sign}${value:.0f}"


def fmt_dur(seconds: float) -> str:
    if seconds < 90:
        return f"{seconds:.0f} dtk"
    if seconds < 5400:
        return f"{seconds / 60:.0f} mnt"
    return f"{seconds / 3600:.1f} jam"


def fmt_pct(value: float | None, digits: int = 0) -> str:
    return "?" if value is None else f"{value:.{digits}f}%"


@dataclass
class Check:
    name: str
    status: str
    detail: str

    def to_dict(self) -> dict:
        return {"name": self.name, "status": self.status, "detail": self.detail}


@dataclass
class FilterResult:
    checks: list[Check] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return all(c.status != FAIL for c in self.checks)

    @property
    def failures(self) -> list[str]:
        return [f"{c.name}: {c.detail}" for c in self.checks if c.status == FAIL]

    def to_dict(self) -> dict:
        return {"passed": self.passed, "checks": [c.to_dict() for c in self.checks]}


def _missing(name: str, what: str, strict: bool) -> Check:
    return Check(name, FAIL if strict else SKIP, f"data {what} belum ada")


def _at_least(name: str, value, minimum, fmt, strict: bool, what: str) -> Check:
    if value is None:
        return _missing(name, what, strict)
    ok = value >= minimum
    return Check(name, OK if ok else FAIL, f"{fmt(value)} {'≥' if ok else '<'} {fmt(minimum)}")


def _at_most(name: str, value, maximum, fmt, strict: bool, what: str) -> Check:
    if value is None:
        return _missing(name, what, strict)
    ok = value <= maximum
    return Check(name, OK if ok else FAIL, f"{fmt(value)} {'≤' if ok else '>'} {fmt(maximum)}")


def market_checks(snap: MarketSnapshot, first_price_usd: float | None, cfg: FilterConfig) -> list[Check]:
    strict = cfg.strict_missing_data
    checks = [
        _at_least("likuiditas", snap.liquidity_usd, cfg.min_liquidity_usd, fmt_usd, strict, "likuiditas"),
    ]
    mcap = snap.market_cap_usd
    if mcap is None:
        checks.append(_missing("mcap", "market cap", strict))
    elif mcap < cfg.min_market_cap_usd:
        checks.append(Check("mcap", FAIL, f"{fmt_usd(mcap)} < {fmt_usd(cfg.min_market_cap_usd)}"))
    elif cfg.max_market_cap_usd > 0 and mcap > cfg.max_market_cap_usd:
        checks.append(Check("mcap", FAIL, f"{fmt_usd(mcap)} > {fmt_usd(cfg.max_market_cap_usd)}"))
    else:
        checks.append(Check("mcap", OK, fmt_usd(mcap)))
    checks.append(_at_least("volume 5m", snap.volume_m5, cfg.min_volume_5m_usd, fmt_usd, strict, "volume"))
    checks.append(_at_least("transaksi 5m", snap.txns_m5, cfg.min_txns_5m, lambda v: f"{v:.0f}", strict, "transaksi"))
    ratio = snap.buy_ratio_m5
    checks.append(
        _at_least(
            "rasio beli 5m",
            None if ratio is None else ratio * 100,
            cfg.min_buy_ratio_5m * 100,
            lambda v: f"{v:.0f}%",
            strict,
            "rasio beli",
        )
    )
    price = snap.price_usd
    if price is None or not first_price_usd:
        checks.append(_missing("harga vs awal", "harga", strict))
    else:
        change = (price / first_price_usd - 1) * 100
        if change < -cfg.max_drop_from_first_pct:
            checks.append(Check("harga vs awal", FAIL, f"turun {-change:.0f}% (maks {cfg.max_drop_from_first_pct:.0f}%)"))
        elif change > cfg.max_rise_from_first_pct:
            checks.append(Check("harga vs awal", FAIL, f"sudah naik {change:.0f}% (maks {cfg.max_rise_from_first_pct:.0f}%)"))
        else:
            checks.append(Check("harga vs awal", OK, f"{change:+.0f}%"))
    if cfg.require_socials:
        has = bool(snap.socials)
        checks.append(Check("sosial", OK if has else FAIL, f"{len(snap.socials)} link" if has else "tidak ada link sosial"))
    return checks


def safety_checks(
    safety: SafetyReport | None,
    rug: RugcheckReport | None,
    gmgn: GmgnInfo | None,
    cfg: FilterConfig,
    rug_cfg: RugcheckConfig,
) -> list[Check]:
    strict = cfg.strict_missing_data
    checks: list[Check] = []
    known = safety is not None and safety.authorities_known
    if cfg.require_mint_renounced:
        if not known:
            checks.append(_missing("mint authority", "mint", strict))
        elif safety.mint_authority:
            checks.append(Check("mint authority", FAIL, "belum dicabut (token bisa dicetak lagi)"))
        else:
            checks.append(Check("mint authority", OK, "sudah dicabut"))
    if cfg.require_freeze_renounced:
        if not known:
            checks.append(_missing("freeze authority", "mint", strict))
        elif safety.freeze_authority:
            checks.append(Check("freeze authority", FAIL, "aktif (token bisa dibekukan)"))
        else:
            checks.append(Check("freeze authority", OK, "tidak ada"))
    if known:
        bad = [e for e in safety.extensions if e in cfg.bad_token2022_extensions]
        if bad:
            checks.append(Check("token-2022", FAIL, "ekstensi berbahaya: " + ", ".join(bad)))

    top10 = safety.top10_pct if safety else None
    top1 = safety.top_holder_pct if safety else None
    checks.append(_at_most("top10 holder", top10, cfg.max_top10_pct, fmt_pct, strict, "holder"))
    checks.append(_at_most("holder terbesar", top1, cfg.max_top_holder_pct, lambda v: fmt_pct(v, 1), strict, "holder"))
    dev = safety.dev_pct if safety else None
    checks.append(_at_most("dev pegang", dev, cfg.max_dev_hold_pct, lambda v: fmt_pct(v, 1), strict, "dev"))

    if rug_cfg.enabled:
        if rug is None:
            checks.append(_missing("RugCheck", "RugCheck", strict))
        else:
            ignore = {r.lower() for r in rug_cfg.ignore_risks}
            danger = [r["name"] for r in rug.risks if r.get("level") == "danger" and r["name"].lower() not in ignore]
            if rug_cfg.reject_danger and danger:
                checks.append(Check("RugCheck", FAIL, "bahaya: " + ", ".join(danger)))
            elif rug_cfg.max_score > 0 and (rug.score_normalised or 0) > rug_cfg.max_score:
                checks.append(Check("RugCheck", FAIL, f"skor {rug.score_normalised:.0f} > {rug_cfg.max_score:.0f}"))
            else:
                score = "?" if rug.score_normalised is None else f"{rug.score_normalised:.0f}"
                checks.append(Check("RugCheck", OK, f"skor {score}, {len(rug.risks)} catatan"))

    if gmgn is not None:
        if cfg.min_holders > 0:
            checks.append(_at_least("holder (GMGN)", gmgn.holders, cfg.min_holders, lambda v: f"{v:.0f}", False, "GMGN"))
        if cfg.min_smart_buys > 0:
            checks.append(
                _at_least("smart money (GMGN)", gmgn.smart_buys, cfg.min_smart_buys, lambda v: f"{v:.0f}", False, "GMGN")
            )
        if cfg.max_sniper_count > 0:
            checks.append(
                _at_most("sniper (GMGN)", gmgn.sniper_count, cfg.max_sniper_count, lambda v: f"{v:.0f}", False, "GMGN")
            )
    return checks
