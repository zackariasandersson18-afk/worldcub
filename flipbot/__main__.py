"""python -m flipbot [sokord ...]

Exempel:
  python -m flipbot "stone island tröja" "arcteryx jacka" --min-profit 200
  python -m flipbot --blocket            # standardlistan, kop pa Vinted + Blocket
"""
from __future__ import annotations

import argparse
import sys

from flipbot import analysis, sources

# Sokord som brukar ga bra i andra hand. Hall dem specifika (marke + plagg),
# annars blandas t.ex. kepsar och jackor i samma referenspris.
DEFAULT_TERMS = [
    'arcteryx jacka', 'stone island tröja', 'patagonia fleece', 'ralph lauren skjorta',
    'carhartt jacka', 'canada goose jacka', 'moncler jacka', 'acne studios jeans',
    'dr martens kängor', 'new balance 550', 'nike dunk', 'salomon xt-6',
    'levis 501', 'fjällräven kånken', 'the north face nuptse', 'barbour jacka',
]


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog='flipbot', description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('terms', nargs='*', help='sokord (standard: en inbyggd lista)')
    p.add_argument('--blocket', action='store_true', help='leta aven inkop pa Blocket')
    p.add_argument('--min-profit', type=float, default=100.0, help='min vinst i SEK (100)')
    p.add_argument('--min-margin', type=float, default=0.3, help='min vinst/kostnad (0.3)')
    p.add_argument('--sell-discount', type=float, default=0.85,
                   help='andel av medianpriset du raknar med att fa (0.85)')
    p.add_argument('--shipping', type=float, default=60.0, help='frakt vid inkop i SEK (60)')
    p.add_argument('--pages', type=int, default=2, help='Vinted-sidor a 96 annonser per sokord (2)')
    p.add_argument('--top', type=int, default=5, help='max fynd per sokord (5)')
    a = p.parse_args(argv)

    http = sources.new_session()
    found = 0
    for term in a.terms or DEFAULT_TERMS:
        try:
            market = sources.vinted_search(term, http, pages=a.pages)
            newest = sources.vinted_search(term, http, pages=1, order='newest_first')
            buy = newest + market
            if a.blocket:
                try:
                    buy += sources.blocket_search(term, http)
                except (sources.SourceError, ValueError) as e:
                    print(f'  (Blocket: {e})', file=sys.stderr)
        except (sources.SourceError, ValueError, OSError) as e:
            print(f'{term}: kunde inte hamta ({e})', file=sys.stderr)
            continue
        deals = analysis.find_deals(term, market, buy, sell_discount=a.sell_discount,
                                    shipping=a.shipping, min_profit=a.min_profit,
                                    min_margin=a.min_margin)
        ref = analysis.reference_price([l for l in market if analysis.relevant(l, term)])
        head = f'== {term}'
        head += f'  (median {ref[0]:.0f} kr av {ref[1]} annonser)' if ref else '  (for fa annonser)'
        print(head)
        for d in deals[:a.top]:
            found += 1
            l = d.listing
            extra = ', '.join(x for x in (l.size, l.condition) if x)
            print(f'  {l.price:>6.0f} kr  ->  ~{d.expected_sale:.0f} kr   vinst ~{d.profit:.0f} kr '
                  f'({d.margin:.0%})  [{l.source}] {l.title}' + (f' ({extra})' if extra else ''))
            print(f'           {l.url}')
    print(f'\n{found} fynd. Kolla alltid bilder, skick och storlek innan du koper.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
