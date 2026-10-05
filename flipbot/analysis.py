"""Raknar fram ett andrahandspris per sokord och hittar annonser under det.

Referenspriset ar medianen av Vinted-annonsernas begarda pris for samma sokord
(efter att irrelevanta och extrema annonser rensats bort). Begarda priser ar
hogre an slutpriser, sa vi raknar med att bara fa `sell_discount` av medianen.
"""
from __future__ import annotations

import re
import statistics
from dataclasses import dataclass

from flipbot.sources import Listing

# Ungefarliga avgifter, justera via CLI-flaggor.
VINTED_BUYER_FEE_PCT = 0.05   # kopskydd nar man sjalv koper pa Vinted
VINTED_BUYER_FEE_FIXED = 7.0  # SEK
VINTED_SELLER_FEE_PCT = 0.0   # saljaren betalar ingen avgift pa Vinted i Sverige

JUNK_WORDS = ('defekt', 'trasig', 'fake', 'kopia', 'replica', 'endast kartong',
              'bara kartong', 'reservdel', 'söker', 'soker', 'köpes', 'kopes')


@dataclass
class Deal:
    listing: Listing
    reference: float      # median av Vinted-priser for sokordet
    expected_sale: float  # det vi raknar med att fa vid forsaljning
    cost: float           # inkopspris inkl. avgifter
    profit: float
    margin: float         # profit / cost
    n_reference: int


def _words(text: str) -> list[str]:
    return re.findall(r'\w+', text.lower())


def relevant(listing: Listing, term: str) -> bool:
    """Alla ord i sokordet maste finnas i titeln eller varumarket."""
    hay = set(_words(listing.title + ' ' + listing.brand))
    return all(w in hay for w in _words(term))


def is_junk(listing: Listing) -> bool:
    t = listing.title.lower()
    return any(w in t for w in JUNK_WORDS)


def reference_price(listings: list[Listing], *, min_count: int = 8) -> tuple[float, int] | None:
    """Median efter att priser utanfor 1.5*IQR tagits bort. None om underlaget ar for litet."""
    prices = sorted(l.price for l in listings if l.price > 0)
    if len(prices) < min_count:
        return None
    q1, _, q3 = statistics.quantiles(prices, n=4)
    lo, hi = q1 - 1.5 * (q3 - q1), q3 + 1.5 * (q3 - q1)
    kept = [p for p in prices if lo <= p <= hi]
    if len(kept) < min_count:
        return None
    return statistics.median(kept), len(kept)


def buy_cost(listing: Listing, *, shipping: float) -> float:
    if listing.source == 'vinted':
        return listing.price * (1 + VINTED_BUYER_FEE_PCT) + VINTED_BUYER_FEE_FIXED + shipping
    return listing.price + shipping


def find_deals(term: str, sell_side: list[Listing], buy_side: list[Listing], *,
               sell_discount: float = 0.85, shipping: float = 60.0,
               min_profit: float = 100.0, min_margin: float = 0.3,
               min_reference: int = 8) -> list[Deal]:
    """Annonser i buy_side som kan saljas vidare pa Vinted med vinst.

    shipping: frakt vi sjalva betalar vid inkop (Vinted-kopare betalar frakten
    nar vi saljer, sa den kostnaden finns bara pa inkopssidan).
    """
    pool = [l for l in sell_side if relevant(l, term) and not is_junk(l)]
    ref = reference_price(pool, min_count=min_reference)
    if ref is None:
        return []
    reference, n = ref
    expected_sale = reference * sell_discount * (1 - VINTED_SELLER_FEE_PCT)
    deals = []
    seen = set()
    for l in buy_side:
        if l.url in seen or l.price <= 0 or not relevant(l, term) or is_junk(l):
            continue
        seen.add(l.url)
        cost = buy_cost(l, shipping=shipping)
        profit = expected_sale - cost
        margin = profit / cost
        if profit >= min_profit and margin >= min_margin:
            deals.append(Deal(l, reference, expected_sale, cost, profit, margin, n))
    deals.sort(key=lambda d: d.profit, reverse=True)
    return deals
