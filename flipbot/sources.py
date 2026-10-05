"""Hamtar annonser fran Vinted och Blocket.

Ingen av sajterna har ett publikt API. Vinted-anropet foljer samma flode som
webbsidan (hamta en session-cookie fran startsidan, sedan /api/v2/catalog/items).
Blocket-anropet anvander sokningens JSON-endpoint pa den nya plattformen och
tolkas defensivt: andras falten hoppas annonsen over i stallet for att krascha.
"""
from __future__ import annotations

from dataclasses import dataclass

import requests

USER_AGENT = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
              '(KHTML, like Gecko) Chrome/126.0 Safari/537.36')
VINTED_BASE = 'https://www.vinted.se'
BLOCKET_SEARCH = 'https://www.blocket.se/recommerce/forsale/search/api/search/SEARCH_ID_BAP_COMMON'


class SourceError(RuntimeError):
    pass


@dataclass
class Listing:
    source: str
    title: str
    price: float          # annonserat pris i SEK
    url: str
    brand: str = ''
    size: str = ''
    condition: str = ''


def _amount(value) -> float | None:
    """Vinted skickar pris som '150.0' eller {'amount': '150.0', ...}."""
    if isinstance(value, dict):
        value = value.get('amount')
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def new_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({'User-Agent': USER_AGENT, 'Accept-Language': 'sv-SE,sv;q=0.9'})
    return s


def vinted_search(term: str, http: requests.Session, *, pages: int = 2,
                  per_page: int = 96, price_to: float | None = None,
                  order: str = 'relevance') -> list[Listing]:
    if not http.cookies.get('access_token_web'):
        http.get(VINTED_BASE + '/', timeout=20)
    out: list[Listing] = []
    for page in range(1, pages + 1):
        params = {'search_text': term, 'page': page, 'per_page': per_page, 'order': order}
        if price_to is not None:
            params['price_to'] = int(price_to)
        r = http.get(VINTED_BASE + '/api/v2/catalog/items', params=params,
                     headers={'Accept': 'application/json'}, timeout=20)
        if r.status_code != 200:
            raise SourceError(f'Vinted svarade {r.status_code} for "{term}"')
        items = r.json().get('items') or []
        for it in items:
            price = _amount(it.get('price'))
            if price is None:
                continue
            url = it.get('url') or f"{VINTED_BASE}/items/{it.get('id')}"
            out.append(Listing('vinted', it.get('title') or '', price, url,
                               brand=it.get('brand_title') or '',
                               size=it.get('size_title') or '',
                               condition=it.get('status') or ''))
        if len(items) < per_page:
            break
    return out


def blocket_search(term: str, http: requests.Session, *, pages: int = 1) -> list[Listing]:
    out: list[Listing] = []
    for page in range(1, pages + 1):
        r = http.get(BLOCKET_SEARCH, params={'q': term, 'page': page},
                     headers={'Accept': 'application/json'}, timeout=20)
        if r.status_code != 200:
            raise SourceError(f'Blocket svarade {r.status_code} for "{term}"')
        docs = r.json().get('docs') or []
        for d in docs:
            price = _amount(d.get('price'))
            title = d.get('heading') or d.get('title')
            url = d.get('canonical_url') or d.get('url')
            if price is None or not title or not url:
                continue
            out.append(Listing('blocket', title, price, url))
        if not docs:
            break
    return out
