"""CLI for VM 2026-bettingboten.

Exempel:
    python -m wc26bot Argentina Frankrike --odds 2.10 3.40 3.20 --bankroll 1000
"""
from __future__ import annotations

import argparse
import sys

from wc26bot.betting import BetOption, best_value_bet, recommend_stake
from wc26bot.data.wc26_teams import WC26_TEAMS
from wc26bot.model import match_probabilities


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Berakna mest sannolikt utfall och basta value bet for en VM 2026-match."
    )
    parser.add_argument("home", help="Hemmalag (se wc26bot/data/wc26_teams.py for namn)")
    parser.add_argument("away", help="Bortalag")
    parser.add_argument(
        "--odds",
        nargs=3,
        type=float,
        metavar=("HEMMA", "OAVGJORT", "BORTA"),
        required=True,
        help="Decimalodds (1X2) fran bookmaker",
    )
    parser.add_argument(
        "--bankroll", type=float, default=1000.0, help="Tillganglig bankrulle"
    )
    parser.add_argument(
        "--kelly-multiplier",
        type=float,
        default=0.5,
        help="Kelly-fraktion (0.5 = half Kelly, lagre risk). Default 0.5.",
    )
    parser.add_argument(
        "--min-edge",
        type=float,
        default=0.02,
        help="Minsta edge (modell vs bookmaker) for att betraktas som value bet.",
    )
    return parser.parse_args(argv)


def run(argv: list[str]) -> int:
    args = parse_args(argv)

    if args.home not in WC26_TEAMS or args.away not in WC26_TEAMS:
        known = ", ".join(sorted(WC26_TEAMS))
        print(f"Okant lag. Kanda lag: {known}", file=sys.stderr)
        return 1

    home_team = WC26_TEAMS[args.home]
    away_team = WC26_TEAMS[args.away]
    probs = match_probabilities(home_team, away_team)

    print(f"\n{args.home} vs {args.away}")
    print(f"Forvantade mal: {probs.home_goals_exp:.2f} - {probs.away_goals_exp:.2f}")
    print(f"1 ({args.home}): {probs.home_win:.1%}")
    print(f"X (Oavgjort):    {probs.draw:.1%}")
    print(f"2 ({args.away}): {probs.away_win:.1%}")
    print(f"Bada lag gor mal: {probs.btts_yes:.1%}")
    print(f"Over 2.5 mal:     {probs.over_2_5:.1%}")

    home_odds, draw_odds, away_odds = args.odds
    options = [
        BetOption(f"1 ({args.home})", probs.home_win, home_odds),
        BetOption("X (Oavgjort)", probs.draw, draw_odds),
        BetOption(f"2 ({args.away})", probs.away_win, away_odds),
    ]

    print("\nJamforelse mot odds:")
    for opt in options:
        print(
            f"  {opt.label:<20} odds={opt.decimal_odds:<6.2f} "
            f"modell={opt.model_probability:.1%} bookmaker={opt.implied_probability:.1%} "
            f"edge={opt.edge:+.1%} EV={opt.expected_value:+.1%}"
        )

    best = best_value_bet(options, min_edge=args.min_edge)
    if best is None:
        print("\nIngen value bet hittades (ingen edge over tradskeln). Rekommendation: avsta.")
        return 0

    stake = recommend_stake(best, args.bankroll, kelly_multiplier=args.kelly_multiplier)
    print(f"\nBasta value bet: {best.label}")
    print(f"  Edge: {best.edge:+.1%}  EV: {best.expected_value:+.1%}")
    print(f"  Rekommenderad insats ({args.kelly_multiplier:.0%} Kelly): {stake:.2f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run(sys.argv[1:]))
