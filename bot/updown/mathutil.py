"""Small numeric helpers (no numpy/scipy dependency)."""
from __future__ import annotations

import math

_FPMIN = 1e-300


def clamp(x: float, lo: float, hi: float) -> float:
    return lo if x < lo else hi if x > hi else x


def norm_cdf(z: float) -> float:
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def _betacf(a: float, b: float, x: float, max_iter: int = 300, eps: float = 3e-14) -> float:
    """Continued fraction for the incomplete beta function (modified Lentz)."""
    qab, qap, qam = a + b, a + 1.0, a - 1.0
    c = 1.0
    d = 1.0 - qab * x / qap
    if abs(d) < _FPMIN:
        d = _FPMIN
    d = 1.0 / d
    h = d
    for m in range(1, max_iter + 1):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        if abs(d) < _FPMIN:
            d = _FPMIN
        c = 1.0 + aa / c
        if abs(c) < _FPMIN:
            c = _FPMIN
        d = 1.0 / d
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        if abs(d) < _FPMIN:
            d = _FPMIN
        c = 1.0 + aa / c
        if abs(c) < _FPMIN:
            c = _FPMIN
        d = 1.0 / d
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < eps:
            break
    return h


def betai(a: float, b: float, x: float) -> float:
    """Regularized incomplete beta function I_x(a, b)."""
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    log_bt = math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b) + a * math.log(x) + b * math.log1p(-x)
    bt = math.exp(log_bt)
    if x < (a + 1.0) / (a + b + 2.0):
        return bt * _betacf(a, b, x) / a
    return 1.0 - bt * _betacf(b, a, 1.0 - x) / b


def student_t_cdf(t: float, dof: float) -> float:
    """CDF of Student's t distribution with `dof` degrees of freedom."""
    if math.isinf(t):
        return 1.0 if t > 0 else 0.0
    x = dof / (dof + t * t)
    tail = 0.5 * betai(dof / 2.0, 0.5, x)
    return 1.0 - tail if t >= 0 else tail


def unit_variance_t_cdf(z: float, dof: float) -> float:
    """P(X <= z) where X is Student-t rescaled to unit variance (dof > 2).

    Same variance as a standard normal, fatter tails: a better fit for
    minute-scale crypto returns than the normal distribution.
    """
    if dof <= 2.0:
        raise ValueError("dof must be > 2 for a finite-variance t distribution")
    return student_t_cdf(z * math.sqrt(dof / (dof - 2.0)), dof)


def floor_to_tick(price: float, tick: float) -> float:
    decimals = max(0, -int(math.floor(math.log10(tick))))
    return round(math.floor(price / tick + 1e-9) * tick, decimals)


def ceil_to_tick(price: float, tick: float) -> float:
    decimals = max(0, -int(math.floor(math.log10(tick))))
    return round(math.ceil(price / tick - 1e-9) * tick, decimals)
