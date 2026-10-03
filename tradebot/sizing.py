"""Position sizing: size for the path, not the destination."""
from __future__ import annotations


def position_size(capital: float, entry: float, stop: float,
                  risk_pct: float = 0.01, max_position_pct: float = 0.20) -> dict:
    """
    risk_pct: fraction of capital lost if the stop hits
    max_position_pct: hard cap on notional as a fraction of capital
    """
    if capital <= 0 or entry <= 0:
        raise ValueError('capital and entry must be positive')
    risk_per_unit = abs(entry - stop)
    if risk_per_unit == 0:
        raise ValueError('stop cannot equal entry')

    units = (capital * risk_pct) / risk_per_unit
    notional = units * entry

    cap = capital * max_position_pct
    if notional > cap:
        units = cap / entry
        notional = cap

    return {
        'units': round(units, 6),
        'notional': round(notional, 2),
        'pct_of_capital': round(notional / capital * 100, 1),
        'loss_if_stopped': round(units * risk_per_unit, 2),
    }


def losing_streak_drawdown(risk_pct: float, n_losses: int) -> float:
    """Drawdown in percent after n consecutive stopped-out trades."""
    return round((1 - (1 - risk_pct) ** n_losses) * 100, 1)
