"""Polymarket daily highest-temperature events: discovery, parsing, prices."""
from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from datetime import date, datetime

import requests

GAMMA = 'https://gamma-api.polymarket.com'
CLOB = 'https://clob.polymarket.com'
TAG = 'highest-temperature'

TITLE_RE = re.compile(r'^Highest temperature in (?P<city>.+?) on (?P<month>[A-Za-z]+) (?P<day>\d{1,2})\??$')
SLUG_DATE_RE = re.compile(r'-on-(?P<month>[a-z]+)-(?P<day>\d{1,2})-(?P<year>\d{4})$')
BUCKET_PATTERNS = (
    (re.compile(r'^(?P<a>-?\d+)°(?P<u>[CF]) or below$'), lambda a, b: (-math.inf, a)),
    (re.compile(r'^(?P<a>-?\d+)°(?P<u>[CF]) or higher$'), lambda a, b: (a, math.inf)),
    (re.compile(r'^(?P<a>-?\d+)-(?P<b>-?\d+)°(?P<u>[CF])$'), lambda a, b: (a, b)),
    (re.compile(r'^(?P<a>-?\d+)°(?P<u>[CF])$'), lambda a, b: (a, a)),
)
MONTHS = {m: i for i, m in enumerate(
    ['january', 'february', 'march', 'april', 'may', 'june', 'july', 'august',
     'september', 'october', 'november', 'december'], 1)}


@dataclass
class Bucket:
    label: str
    lo: float           # whole degrees, inclusive; -inf for "or below"
    hi: float           # whole degrees, inclusive; +inf for "or higher"
    yes_token: str
    no_token: str
    resolved_yes: bool | None = None   # None while open
    best_bid: float | None = None
    best_ask: float | None = None
    fee_rate: float = 0.05

    def center(self) -> float:
        """Best point estimate of the realized high when this bucket won."""
        if math.isinf(self.lo):
            return self.hi
        if math.isinf(self.hi):
            return self.lo
        return (self.lo + self.hi) / 2


@dataclass
class Event:
    id: str
    slug: str
    city: str
    date: date
    station: str | None
    unit: str            # 'C' or 'F'
    closed: bool
    buckets: list[Bucket] = field(default_factory=list)

    @property
    def winner(self) -> Bucket | None:
        won = [b for b in self.buckets if b.resolved_yes]
        return won[0] if len(won) == 1 else None


def parse_bucket(label: str) -> tuple[float, float, str] | None:
    label = label.strip().replace('º', '°')
    for rx, bounds in BUCKET_PATTERNS:
        m = rx.match(label)
        if m:
            a = float(m.group('a'))
            b = float(m.group('b')) if 'b' in m.groupdict() and m.group('b') else None
            lo, hi = bounds(a, b)
            return lo, hi, m.group('u')
    return None


def station_from_source(url: str | None) -> str | None:
    """weather.gov ...?site=lemd -> LEMD; wunderground .../ZSJN -> ZSJN."""
    if not url:
        return None
    m = re.search(r'[?&]site=([A-Za-z0-9]{3,4})', url)
    if m:
        return m.group(1).upper()
    m = re.search(r'/([A-Za-z]{4})/?$', url)
    return m.group(1).upper() if m else None


def _json_list(v):
    if isinstance(v, str):
        try:
            return json.loads(v)
        except ValueError:
            return None
    return v


def parse_event(e: dict) -> Event | None:
    title = (e.get('title') or '').strip()
    tm = TITLE_RE.match(title)
    dm = SLUG_DATE_RE.search(e.get('slug') or '')
    if not tm or not dm:
        return None
    try:
        day = date(int(dm.group('year')), MONTHS[dm.group('month')], int(dm.group('day')))
    except (KeyError, ValueError):
        return None
    buckets, units = [], set()
    for m in e.get('markets') or []:
        parsed = parse_bucket(m.get('groupItemTitle') or '')
        tokens = _json_list(m.get('clobTokenIds')) or []
        if not parsed or len(tokens) != 2:
            return None  # an unparseable bucket would make probabilities wrong
        lo, hi, unit = parsed
        units.add(unit)
        prices = _json_list(m.get('outcomePrices'))
        resolved = None
        if m.get('closed') and prices and len(prices) == 2:
            yes = float(prices[0])
            resolved = True if yes >= 0.99 else False if yes <= 0.01 else None
        fee = (m.get('feeSchedule') or {}).get('rate', 0.05)
        buckets.append(Bucket(m.get('groupItemTitle'), lo, hi, str(tokens[0]), str(tokens[1]),
                              resolved, m.get('bestBid'), m.get('bestAsk'), float(fee or 0)))
    if not buckets or len(units) != 1:
        return None
    buckets.sort(key=lambda b: b.lo)
    return Event(str(e.get('id')), e.get('slug'), tm.group('city'), day,
                 station_from_source(e.get('resolutionSource')), units.pop(),
                 bool(e.get('closed')), buckets)


def fetch_events(closed: bool, pages: int = 1, page_size: int = 100,
                 session: requests.Session | None = None, since: date | None = None) -> list[Event]:
    """Newest first. Stops at `since`, at the last page, or where Gamma stops
    paginating (it answers 422 past its maximum offset)."""
    http = session or requests.Session()
    out = []
    for page in range(pages):
        r = http.get(f'{GAMMA}/events', params={
            'tag_slug': TAG, 'closed': str(closed).lower(), 'limit': page_size,
            'offset': page * page_size, 'order': 'endDate', 'ascending': 'false'}, timeout=30)
        if r.status_code == 422 and page > 0:
            break
        r.raise_for_status()
        batch = r.json()
        parsed = [ev for ev in map(parse_event, batch) if ev]
        out += parsed
        if len(batch) < page_size or (since and parsed and max(e.date for e in parsed) < since):
            break
    return out


def price_history(token: str, session: requests.Session | None = None) -> list[tuple[int, float]]:
    http = session or requests.Session()
    r = http.get(f'{CLOB}/prices-history', params={'market': token, 'interval': 'max', 'fidelity': 60},
                 timeout=30)
    r.raise_for_status()
    return [(int(p['t']), float(p['p'])) for p in r.json().get('history', [])]


def price_at(history: list[tuple[int, float]], ts: int, max_age_s: int = 6 * 3600) -> float | None:
    """Last traded/mid price at or before ts -- never a later one."""
    before = [p for t, p in history if t <= ts and ts - t <= max_age_s]
    return before[-1] if before else None


def best_quotes(token: str, session: requests.Session | None = None) -> tuple[float | None, float | None, float]:
    """(best bid, best ask, size at best ask) from the live order book."""
    http = session or requests.Session()
    r = http.get(f'{CLOB}/book', params={'token_id': token}, timeout=30)
    r.raise_for_status()
    book = r.json()
    bids = [(float(x['price']), float(x['size'])) for x in book.get('bids', [])]
    asks = [(float(x['price']), float(x['size'])) for x in book.get('asks', [])]
    bid = max(bids)[0] if bids else None
    ask = min(asks) if asks else None
    return bid, (ask[0] if ask else None), (ask[1] if ask else 0.0)


def decision_ts(day: date, utc_offset_s: int, local_hour: int = 6) -> int:
    """Unix time of `local_hour`:00 local time on `day`."""
    midnight_utc = datetime(day.year, day.month, day.day).timestamp() - datetime(1970, 1, 1).timestamp()
    return int(midnight_utc + local_hour * 3600 - utc_offset_s)
