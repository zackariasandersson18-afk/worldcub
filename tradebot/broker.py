"""Order execution: a local paper broker and the Binance SPOT TESTNET.

Real money is deliberately not supported here. BinanceTestnetBroker
refuses any base URL that is not the testnet.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
import time
from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal
from pathlib import Path
from urllib.parse import urlencode

import requests

TESTNET_URL = 'https://testnet.binance.vision'


@dataclass
class SymbolRules:
    base: str
    quote: str
    step_size: float = 1e-6
    min_qty: float = 0.0
    min_notional: float = 10.0

    def round_qty(self, qty: float) -> float:
        """Round DOWN to the exchange's step size (never buy more than intended)."""
        step = Decimal(str(self.step_size))
        return float((Decimal(str(qty)) / step).to_integral_value(ROUND_DOWN) * step)


@dataclass
class Fill:
    side: str
    qty: float
    price: float
    quote_qty: float
    commission_quote: float


def split_symbol(symbol: str) -> tuple[str, str]:
    for quote in ('USDT', 'USDC', 'FDUSD', 'BUSD', 'EUR', 'BTC', 'ETH'):
        if symbol.endswith(quote) and len(symbol) > len(quote):
            return symbol[:-len(quote)], quote
    raise ValueError(f'cannot split symbol {symbol!r} into base/quote')


class PaperBroker:
    """Simulated spot account kept in a JSON file. Fills at the given price
    plus slippage and fee, the same costs the backtest charges."""

    def __init__(self, path: str | Path, symbol: str, initial_quote: float = 10_000,
                 fee_bps: float = 5.0, slippage_bps: float = 3.0):
        self.path = Path(path)
        self.symbol = symbol
        base, quote = split_symbol(symbol)
        self._rules = SymbolRules(base, quote)
        self.fee = fee_bps / 1e4
        self.slip = slippage_bps / 1e4
        if self.path.exists():
            self.wallet = json.loads(self.path.read_text())
        else:
            self.wallet = {base: 0.0, quote: float(initial_quote)}
            self.path.write_text(json.dumps(self.wallet, indent=2))
        self.last_price: float | None = None

    def rules(self) -> SymbolRules:
        return self._rules

    def set_price(self, price: float) -> None:
        self.last_price = price

    def price(self) -> float:
        if self.last_price is None:
            raise RuntimeError('paper broker has no price; call set_price first')
        return self.last_price

    def balances(self) -> dict[str, float]:
        return dict(self.wallet)

    def market_order(self, side: str, qty: float) -> Fill:
        r = self._rules
        px = self.price() * (1 + self.slip if side == 'BUY' else 1 - self.slip)
        quote_qty = qty * px
        fee = quote_qty * self.fee
        if side == 'BUY':
            if quote_qty + fee > self.wallet[r.quote] + 1e-9:
                raise ValueError('insufficient quote balance')
            self.wallet[r.quote] -= quote_qty + fee
            self.wallet[r.base] += qty
        else:
            if qty > self.wallet[r.base] + 1e-12:
                raise ValueError('insufficient base balance')
            self.wallet[r.base] -= qty
            self.wallet[r.quote] += quote_qty - fee
        self.path.write_text(json.dumps(self.wallet, indent=2))
        return Fill(side, qty, px, quote_qty, fee)


class BinanceTestnetBroker:
    """Binance Spot Testnet. Keys from https://testnet.binance.vision
    (env BINANCE_TESTNET_API_KEY / BINANCE_TESTNET_API_SECRET)."""

    def __init__(self, symbol: str, api_key: str | None = None,
                 api_secret: str | None = None, base_url: str = TESTNET_URL,
                 session: requests.Session | None = None, recv_window: int = 5000):
        if 'testnet' not in base_url:
            raise ValueError('only the Binance testnet is supported; refusing ' + base_url)
        self.symbol = symbol
        self.base_url = base_url.rstrip('/')
        self.api_key = api_key or os.environ.get('BINANCE_TESTNET_API_KEY')
        self.api_secret = api_secret or os.environ.get('BINANCE_TESTNET_API_SECRET')
        if not self.api_key or not self.api_secret:
            raise ValueError('set BINANCE_TESTNET_API_KEY and BINANCE_TESTNET_API_SECRET')
        self.http = session or requests.Session()
        self.recv_window = recv_window
        self._rules: SymbolRules | None = None

    def _request(self, method: str, path: str, params: dict | None = None,
                 signed: bool = False) -> dict | list:
        params = dict(params or {})
        headers = {}
        if signed:
            params['recvWindow'] = self.recv_window
            params['timestamp'] = int(time.time() * 1000)
            query = urlencode(params)
            sig = hmac.new(self.api_secret.encode(), query.encode(),
                           hashlib.sha256).hexdigest()
            params['signature'] = sig
            headers['X-MBX-APIKEY'] = self.api_key
        resp = self.http.request(method, f'{self.base_url}{path}',
                                 params=params, headers=headers, timeout=20)
        if resp.status_code >= 400:
            raise RuntimeError(f'Binance {resp.status_code}: {resp.text}')
        return resp.json()

    def rules(self) -> SymbolRules:
        if self._rules is None:
            info = self._request('GET', '/api/v3/exchangeInfo', {'symbol': self.symbol})
            sym = info['symbols'][0]
            filters = {f['filterType']: f for f in sym['filters']}
            lot = filters.get('MARKET_LOT_SIZE') or filters.get('LOT_SIZE') or {}
            if float(lot.get('stepSize', 0)) == 0:
                lot = filters.get('LOT_SIZE', {})
            notional = filters.get('NOTIONAL') or filters.get('MIN_NOTIONAL') or {}
            self._rules = SymbolRules(
                base=sym['baseAsset'], quote=sym['quoteAsset'],
                step_size=float(lot.get('stepSize', 1e-6)),
                min_qty=float(lot.get('minQty', 0)),
                min_notional=float(notional.get('minNotional', 0)),
            )
        return self._rules

    def price(self) -> float:
        data = self._request('GET', '/api/v3/ticker/price', {'symbol': self.symbol})
        return float(data['price'])

    def balances(self) -> dict[str, float]:
        data = self._request('GET', '/api/v3/account', signed=True)
        return {b['asset']: float(b['free']) for b in data['balances']}

    def market_order(self, side: str, qty: float) -> Fill:
        data = self._request('POST', '/api/v3/order', {
            'symbol': self.symbol, 'side': side, 'type': 'MARKET',
            'quantity': f'{qty:.8f}'.rstrip('0').rstrip('.'),
            'newOrderRespType': 'FULL',
        }, signed=True)
        executed = float(data['executedQty'])
        quote_qty = float(data['cummulativeQuoteQty'])
        avg = quote_qty / executed if executed else math.nan
        base, quote = self.rules().base, self.rules().quote
        commission = 0.0
        for f in data.get('fills', []):
            c = float(f['commission'])
            if f['commissionAsset'] == quote:
                commission += c
            elif f['commissionAsset'] == base:
                commission += c * float(f['price'])
        return Fill(side, executed, avg, quote_qty, commission)
