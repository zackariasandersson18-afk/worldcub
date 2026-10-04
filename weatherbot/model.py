"""Bucket probabilities, calibration and bet selection.

Pre-registered before any backtest result was seen (round 3, +1 trial -> 48):
  * mu     = equal-weight mean of the models' daily highs + learned bias
  * sigma  = std of past residuals (pooled over cities, expanding window,
             only days strictly before the trading day), floor 0.7 C
  * settlement whole degrees: P(lo <= round(T) <= hi)
  * trade YES when p - cost >= MIN_EDGE, NO when (1 - p) - cost_no >= MIN_EDGE,
    cost = price + SLIPPAGE + fee, fee = rate * min(price, 1 - price) per share
  * size: KELLY_FRACTION x Kelly, at most MAX_BET of bankroll per bet and
    MAX_EVENT per city-day; prices outside [MIN_PRICE, MAX_PRICE] are skipped
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date

from scipy.stats import norm

MIN_SIGMA_C = 0.7
MIN_RESIDUALS = 50
MIN_EDGE = 0.05
SLIPPAGE = 0.01
KELLY_FRACTION = 0.25
MAX_BET = 0.02
MAX_EVENT = 0.05
MIN_PRICE, MAX_PRICE = 0.02, 0.98
C_PER_F = 5 / 9


def bucket_prob(mu: float, sigma: float, lo: float, hi: float) -> float:
    """P(lo <= round(T) <= hi) for T ~ N(mu, sigma)."""
    upper = 1.0 if math.isinf(hi) else norm.cdf((hi + 0.5 - mu) / sigma)
    lower = 0.0 if math.isinf(lo) else norm.cdf((lo - 0.5 - mu) / sigma)
    return max(0.0, upper - lower)


def fee_per_share(price: float, rate: float) -> float:
    # conservative reading of Polymarket's weather fee (taker only)
    return rate * min(price, 1.0 - price)


class Calibration:
    """Residuals (realized high - blended forecast), stored in degrees C."""

    def __init__(self):
        self.rows: list[tuple[date, float]] = []

    def add(self, day: date, residual: float, unit: str) -> None:
        self.rows.append((day, residual * (C_PER_F if unit == 'F' else 1.0)))

    def params(self, before: date, unit: str) -> tuple[float, float] | None:
        """(bias, sigma) in the market's unit from days strictly before `before`."""
        past = [r for d, r in self.rows if d < before]
        if len(past) < MIN_RESIDUALS:
            return None
        bias = sum(past) / len(past)
        sd = math.sqrt(sum((r - bias) ** 2 for r in past) / (len(past) - 1))
        scale = 1.0 / C_PER_F if unit == 'F' else 1.0
        return bias * scale, max(sd, MIN_SIGMA_C) * scale


@dataclass
class Bet:
    bucket: str
    side: str        # 'YES' or 'NO'
    price: float     # quoted price of the token bought
    cost: float      # price + slippage + fee per share
    prob: float      # model probability that the token pays 1
    edge: float
    stake: float     # fraction of bankroll


def select_bets(buckets, probs: list[float], prices: list[float | None]) -> list[Bet]:
    """Backtest version: one YES price per bucket (None = no quote); NO costs 1 - price."""
    return select_bets_quotes(buckets, probs, prices,
                              [None if p is None else 1 - p for p in prices])


def select_bets_quotes(buckets, probs: list[float], yes_asks: list[float | None],
                       no_asks: list[float | None]) -> list[Bet]:
    """Live version: buy each side at its own best ask (the price actually payable)."""
    bets = []
    for b, p, ya, na in zip(buckets, probs, yes_asks, no_asks):
        for side, prob, price in (('YES', p, ya), ('NO', 1 - p, na)):
            if price is None or not MIN_PRICE <= price <= MAX_PRICE:
                continue
            cost = price + SLIPPAGE + fee_per_share(price, b.fee_rate)
            edge = prob - cost
            if edge < MIN_EDGE or cost >= 1:
                continue
            kelly = (prob - cost) / (1 - cost)
            bets.append(Bet(b.label, side, price, cost, prob, edge,
                            min(KELLY_FRACTION * kelly, MAX_BET)))
    total = sum(x.stake for x in bets)
    if total > MAX_EVENT:
        for x in bets:
            x.stake *= MAX_EVENT / total
    return bets


def settle(bet: Bet, won: bool) -> float:
    """PnL as a fraction of bankroll."""
    shares = bet.stake / bet.cost
    return shares * (1.0 if won else 0.0) - bet.stake
