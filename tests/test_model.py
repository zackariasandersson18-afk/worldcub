from wc26bot.model import TeamStrength, match_probabilities


def test_probabilities_sum_to_one():
    home = TeamStrength("A", attack=1.2, defense=0.9)
    away = TeamStrength("B", attack=1.0, defense=1.0)
    probs = match_probabilities(home, away)
    total = probs.home_win + probs.draw + probs.away_win
    assert abs(total - 1.0) < 1e-4


def test_stronger_home_team_favoured():
    strong = TeamStrength("Strong", attack=1.6, defense=0.6)
    weak = TeamStrength("Weak", attack=0.7, defense=1.3)
    probs = match_probabilities(strong, weak)
    assert probs.home_win > probs.away_win
    assert probs.home_win > probs.draw


def test_equal_teams_symmetric_ish():
    a = TeamStrength("A", attack=1.0, defense=1.0)
    b = TeamStrength("B", attack=1.0, defense=1.0)
    probs = match_probabilities(a, b)
    # Home advantage comes only from league_avg_home/away_goals, so home
    # should still be slightly favoured with default params.
    assert probs.home_win > probs.away_win
