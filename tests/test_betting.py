from wc26bot.betting import BetOption, best_value_bet, recommend_stake


def test_edge_and_ev_calculation():
    # Modell sager 50% men bookmaker prisar in 40% (odds 2.5) -> tydlig value
    opt = BetOption("Test", model_probability=0.5, decimal_odds=2.5)
    assert abs(opt.implied_probability - 0.4) < 1e-9
    assert abs(opt.edge - 0.1) < 1e-9
    assert abs(opt.expected_value - 0.25) < 1e-9


def test_best_value_bet_picks_highest_ev():
    low_ev = BetOption("Low", model_probability=0.5, decimal_odds=2.05)
    high_ev = BetOption("High", model_probability=0.5, decimal_odds=2.8)
    no_edge = BetOption("NoEdge", model_probability=0.3, decimal_odds=3.0)
    best = best_value_bet([low_ev, high_ev, no_edge], min_edge=0.0)
    assert best is high_ev


def test_best_value_bet_returns_none_when_no_edge():
    opt = BetOption("Fair", model_probability=0.4, decimal_odds=2.4)
    assert best_value_bet([opt], min_edge=0.01) is None


def test_kelly_fraction_never_negative():
    bad_bet = BetOption("Bad", model_probability=0.2, decimal_odds=2.0)
    assert bad_bet.kelly_fraction() == 0.0


def test_recommend_stake_respects_cap():
    opt = BetOption("Great", model_probability=0.9, decimal_odds=3.0)
    stake = recommend_stake(opt, bankroll=1000, kelly_multiplier=1.0, max_stake_fraction=0.05)
    assert stake <= 50.0
