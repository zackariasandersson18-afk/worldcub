"""Value-bet-detektering och insatsstorlek (Kelly criterion)."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class BetOption:
    label: str
    model_probability: float
    decimal_odds: float

    @property
    def implied_probability(self) -> float:
        return 1 / self.decimal_odds

    @property
    def edge(self) -> float:
        """Modellens overdrag mot bookmakerns implicita sannolikhet."""
        return self.model_probability - self.implied_probability

    @property
    def expected_value(self) -> float:
        """EV per satsad enhet: p*odds - 1."""
        return self.model_probability * self.decimal_odds - 1

    def kelly_fraction(self, kelly_multiplier: float = 1.0) -> float:
        """Andel av bankrullen att satsa enligt (fraktionerad) Kelly-formel.

        f* = (b*p - q) / b  dar b = odds-1, p = sannolikhet, q = 1-p.
        Negativt resultat clampas till 0 (satsa inte).
        """
        b = self.decimal_odds - 1
        if b <= 0:
            return 0.0
        p = self.model_probability
        q = 1 - p
        f = (b * p - q) / b
        return max(0.0, f * kelly_multiplier)


def best_value_bet(
    options: list[BetOption], min_edge: float = 0.0
) -> BetOption | None:
    """Valjer alternativet med hogst EV bland de som har edge >= min_edge."""
    candidates = [o for o in options if o.edge >= min_edge and o.expected_value > 0]
    if not candidates:
        return None
    return max(candidates, key=lambda o: o.expected_value)


def recommend_stake(
    option: BetOption,
    bankroll: float,
    kelly_multiplier: float = 0.5,
    max_stake_fraction: float = 0.05,
) -> float:
    """Foreslar en satsning i valutaenheter.

    kelly_multiplier < 1 (t.ex. 0.5 = "half Kelly") minskar varians/risk for
    forlust jamfort med full Kelly, vilket ar standardpraxis for att begransa
    nedsidan vid modellosakerhet.
    max_stake_fraction satter ett absolut tak per satsning oavsett Kelly-resultat.
    """
    fraction = min(option.kelly_fraction(kelly_multiplier), max_stake_fraction)
    return round(bankroll * fraction, 2)
