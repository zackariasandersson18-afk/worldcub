from rewardbot.backtest import book_q, expected_reward, qmin, score, simulate_fills


def test_score_matches_polymarket_formula():
    assert score(0, 3.0, 100) == 100 and abs(score(1.5, 3.0, 100) - 25) < 1e-9 and score(3.0, 3.0, 100) == 0
    assert qmin(90, 30) == 30 and qmin(90, 0) == 30          # one-sided counts a third


def test_reward_share_falls_with_competition():
    alone = expected_reward(10.0, 3.0, 100, 1.0, (0.0, 0.0))
    crowded = expected_reward(10.0, 3.0, 100, 1.0, (1000.0, 1000.0))
    assert abs(alone - 10.0) < 1e-9 and crowded < alone / 10


def test_book_q_counts_yes_bids_and_no_asks_on_the_same_side():
    yes = {'bids': [{'price': '0.49', 'size': '100'}], 'asks': [{'price': '0.51', 'size': '100'}]}
    no = {'bids': [], 'asks': [{'price': '0.51', 'size': '100'}]}      # NO ask 0.51 = YES bid 0.49
    q1, q2 = book_q(yes, no, 0.50, 3.0)
    assert abs(q1 - 2 * q2) < 1e-9


def test_fills_only_through_the_quote_and_marks_daily():
    t0 = 1_790_000_000 // 86400 * 86400
    trades = [(t0 + 10, 0.50, 5)] + [(t0 + 3600 * k + 10, 0.47 if k % 2 else 0.53, 50) for k in range(1, 24)]
    pnl = simulate_fills(trades, t0, t0 + 2 * 86400, 0.02, 10)
    assert pnl.sum() > 0
    flat = [(t0 + 10, 0.50, 5)] + [(t0 + 3600 * k + 10, 0.48, 50) for k in range(1, 24)]
    assert simulate_fills(flat, t0, t0 + 86400, 0.02, 10).sum() == 0     # touching is not a fill


def test_live_step_freezes_portfolio_accrues_reward_and_fills(tmp_path, monkeypatch):
    from rewardbot import backtest as bt, live
    port = [{'cond': 'c1', 'question': 'Q', 'yes': 'y', 'no': 'n', 'rate': 24.0, 'v': 4.0, 'min_size': 10.0,
             'd': 0.02, 'asset': '0x2791bca1f2de4661ed88a30c99a7a9449aa84174', 'collateral': 9.6,
             'est_reward_day': 24.0, 'end': '2027-01-01'}]
    monkeypatch.setattr(live, 'choose', lambda http, log: port)
    book = {'y': {'bids': [{'price': '0.49', 'size': '5'}], 'asks': [{'price': '0.51', 'size': '5'}]},
            'n': {'bids': [], 'asks': []}}
    monkeypatch.setattr(bt, 'fetch_books', lambda toks, http: book)
    t0 = 1_790_000_000
    monkeypatch.setattr(bt, 'fetch_trades', lambda c, y, since, http: [(t0 + 100, 0.47, 50)])
    live.step(tmp_path, now=t0, http=object())
    tick = live.step(tmp_path, now=t0 + 3600, http=object())
    # alone in the book (competitor levels below min size): full share, 1 h of a 24/day pool
    assert abs(tick['reward_tick'] - 1.0) < 1e-9
    st = __import__('json').loads((tmp_path / 'rewards_state.json').read_text())
    assert st['positions']['c1']['inv'] == 10.0 and st['portfolio'] == port     # bid 0.48 filled, portfolio frozen


def test_realistic_ledger_rules(tmp_path, monkeypatch):
    from rewardbot import backtest as bt, live
    port = [{'cond': 'c1', 'question': 'Q', 'yes': 'y', 'no': 'n', 'rate': 24.0, 'v': 4.0, 'min_size': 10.0,
             'd': 0.02, 'asset': '0x2791bca1f2de4661ed88a30c99a7a9449aa84174', 'collateral': 9.6,
             'est_reward_day': 24.0, 'end': '2027-01-01'}]
    monkeypatch.setattr(live, 'choose', lambda http, log: port)
    books = {}
    monkeypatch.setattr(bt, 'fetch_books', lambda toks, http: books)

    def at(mid, comp=0.0):
        books.clear()
        books.update({'y': {'bids': [{'price': f'{mid - 0.01:.2f}', 'size': str(comp or 5)}],
                            'asks': [{'price': f'{mid + 0.01:.2f}', 'size': str(comp or 5)}]},
                      'n': {'bids': [{'price': f'{1 - mid - 0.01:.2f}', 'size': str(comp or 5)}],
                            'asks': [{'price': f'{1 - mid + 0.01:.2f}', 'size': str(comp or 5)}]}})
    t0 = 1_790_000_000
    trades = []
    monkeypatch.setattr(bt, 'fetch_trades', lambda c, y, since, http: trades)
    at(0.50)
    live.step(tmp_path, now=t0, http=object())
    # 1 h later: alone, nothing moved -> both ledgers earn the full hour; a touch of our bid fills only R4
    trades[:] = [(t0 + 10, 0.48, 50)]
    tick = live.step(tmp_path, now=t0 + 3600, http=object())
    assert abs(tick['reward_tick'] - 1.0) < 1e-9 and abs(tick['real_reward_total'] - 1.0) < 1e-9
    assert tick['fills_total'] == 0 and tick['real_fills_total'] == 1
    # competitors arrive: realistic uses the lower (new) share, optimistic the new one too
    trades[:] = []
    at(0.50, comp=1000)
    t2 = live.step(tmp_path, now=t0 + 7200, http=object())
    assert t2['real_reward_total'] - 1.0 <= t2['reward_tick'] + 1e-9
    # the midpoint jumps 5 cents (> v - d = 2): optimistic still earns, realistic does not
    at(0.55)
    t3 = live.step(tmp_path, now=t0 + 10800, http=object())
    assert t3['reward_tick'] > 0 and abs(t3['real_reward_total'] - t2['real_reward_total']) < 1e-9
    # the book disappears: values are kept, not dropped
    books.clear()
    t4 = live.step(tmp_path, now=t0 + 14400, http=object())
    assert abs(t4['real_mm_value'] - t3['real_mm_value']) < 1e-9 and abs(t4['mm_value'] - t3['mm_value']) < 1e-9
