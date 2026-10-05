from unittest.mock import MagicMock

from flipbot import analysis, sources
from flipbot.sources import Listing


def vinted(title, price, brand=''):
    return Listing('vinted', title, price, f'https://www.vinted.se/items/{title}-{price}', brand=brand)


def market(term_title='Stone Island tröja', prices=(800, 850, 900, 950, 1000, 1000, 1100, 1200, 9000)):
    return [vinted(term_title, p) for p in prices]


def test_relevant_requires_all_words():
    assert analysis.relevant(vinted('Stone Island tröja grå', 500), 'stone island tröja')
    assert analysis.relevant(vinted('Tröja', 500, brand='Stone Island'), 'stone island tröja')
    assert not analysis.relevant(vinted('Stone Island keps', 500), 'stone island tröja')


def test_reference_price_drops_outliers_and_needs_enough_data():
    ref, n = analysis.reference_price(market())
    assert n == 8 and ref == 975
    assert analysis.reference_price(market(prices=(100, 200))) is None


def test_find_deals_flags_underpriced_listing_after_fees():
    cheap = vinted('Stone Island tröja', 300)
    deals = analysis.find_deals('stone island tröja', market(), market() + [cheap], shipping=60)
    assert [d.listing for d in deals] == [cheap]
    d = deals[0]
    assert d.expected_sale == 975 * 0.85
    assert d.cost == 300 * 1.05 + 7 + 60
    assert round(d.profit, 2) == round(975 * 0.85 - (315 + 67), 2)


def test_find_deals_skips_junk_and_irrelevant():
    buy = [vinted('Stone Island tröja defekt', 100), vinted('Stone Island keps', 100)]
    assert analysis.find_deals('stone island tröja', market(), buy) == []


def test_vinted_search_parses_both_price_formats():
    http = MagicMock()
    http.cookies.get.return_value = 'token'
    resp = MagicMock(status_code=200)
    resp.json.return_value = {'items': [
        {'id': 1, 'title': 'A', 'price': '150.0', 'url': 'u1', 'brand_title': 'Nike'},
        {'id': 2, 'title': 'B', 'price': {'amount': '200.0', 'currency_code': 'SEK'}},
        {'id': 3, 'title': 'C', 'price': None},
    ]}
    http.get.return_value = resp
    out = sources.vinted_search('nike', http, pages=1)
    assert [(l.title, l.price) for l in out] == [('A', 150.0), ('B', 200.0)]
    assert out[1].url == 'https://www.vinted.se/items/2'
