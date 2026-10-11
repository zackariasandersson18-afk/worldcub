import json

from rewardbot import census


def mk(cid, rate):
    return {'condition_id': cid, 'question': 'Q ' + cid, 'rewards_config': [{'rate_per_day': rate}]}


def test_hourly_pot_changes_and_daily_entries(tmp_path):
    (tmp_path / 'rewards_v4_state.json').write_text(json.dumps({'portfolio': [{'cond': '0xaaaaaaaaaaaaaaaa'}]}))
    t0 = 1_791_000_000
    first = [mk('0xaaaaaaaaaaaaaaaa', 40), mk('0xbbbbbbbbbbbbbbbb', 20), mk('0xcccccccccccccccc', 1)]
    h = census.maybe_run(str(tmp_path), now=t0, log=lambda *_: None, listing=first)
    assert h['baseline'] and h['n_10plus'] == 2
    assert census.maybe_run(str(tmp_path), now=t0 + 600, log=lambda *_: None, listing=first) is None   # at most hourly
    second = [mk('0xaaaaaaaaaaaaaaaa', 3), mk('0xbbbbbbbbbbbbbbbb', 25), mk('0xdddddddddddddddd', 50)]
    h = census.maybe_run(str(tmp_path), now=t0 + 86400, log=lambda *_: None, listing=second)
    c = json.loads((tmp_path / census.FILE).read_text())
    kinds = {e['id'][:4]: (e['kind'], e['v4']) for e in c['events']}
    assert kinds == {'0xaa': ('ner', True), '0xbb': ('upp', False), '0xdd': ('över 10', False)}
    assert [e['to'] for e in c['events'] if e['id'].startswith('0xaa')] == [3]
    assert h['v4_changes'] == 1 and c['days'][-1]['new'] == 1 and c['days'][-1]['gone'] == 1


def test_needs_a_fresh_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(census.bt, 'LISTING_CACHE', None)
    assert census.maybe_run(str(tmp_path), log=lambda *_: None) is None
