"""Prompts for the five roles. The generator is the least important one."""
from __future__ import annotations

ROLES = {
    'hypothesis': 'Give me a mechanism, not a pattern.',
    'code': 'Shift signals. Include costs. No loops.',
    'critic': 'Find every way this backtest lies.',
    'statistician': 'Deflate the Sharpe for N trials.',
    'risk': 'Size for the path, not the destination.',
}

CRITIC = """\
Review this backtest for the following errors. For each one,
state PRESENT or ABSENT and quote the line.

1. Look-ahead: is the signal shifted before becoming a position?
2. Survivorship: does the asset list include delisted tickers?
3. Repainting: does any indicator use future data (centered
   moving averages, zigzag, unshifted resample)?
4. Costs: are fees AND slippage applied on turnover?
5. Fill assumption: does it assume execution at a price that
   was never actually available?
6. Parameter fitting: how many parameters, and were they
   chosen by looking at the whole dataset?
7. Sample: does the test period contain both a bull and a
   bear regime?
8. Data alignment: are all series on the same timezone and
   bar-close convention?

Do not summarize. Quote lines.
"""

HYPOTHESIS = """\
Propose a testable trading hypothesis for {asset} on the {timeframe}
timeframe. State the ECONOMIC MECHANISM -- who is on the
other side and why they lose. If you cannot name the
counterparty, the idea is a pattern, not an edge.
Then write the exact entry, exit, and invalidation rules.
"""

ADVERSARIAL = """\
You are a quant risk manager whose job is to reject this
strategy. Find every reason it would fail in live trading
that a backtest cannot show. Be specific and hostile.
Assume the author is fooling themselves.
"""

REGIME = """\
Split this backtest into bull, bear, and chop regimes using
a 200-period moving average. Report metrics per regime.
If the edge exists in only one regime, say so directly.
"""

ACCOUNTING = """\
I have tested {n_trials} variations of this idea. Compute the
deflated Sharpe ratio for the best result. Tell me plainly
whether this is distinguishable from noise.
"""

TEMPLATES = {
    'critic': CRITIC,
    'hypothesis': HYPOTHESIS,
    'adversarial': ADVERSARIAL,
    'regime': REGIME,
    'accounting': ACCOUNTING,
}


def render(name: str, **kwargs) -> str:
    defaults = {'asset': 'BTC', 'timeframe': '4H', 'n_trials': 'N'}
    return TEMPLATES[name].format(**{**defaults, **kwargs})
