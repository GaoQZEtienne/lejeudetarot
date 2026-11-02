from .cards import BOUTS, points2
from .contracts import CONTRACTS

TARGETS = (56, 51, 41, 36)


def score_numbers(points, bouts, taker, petit=0, slam=0, multiplier=1):
    target = TARGETS[bouts]
    delta = points - target
    base = 25 + delta if delta >= 0 else -(25 + abs(delta))
    unit = (base + petit) * multiplier + slam
    scores = [-unit] * 4
    scores[taker] = 3 * unit
    return dict(points=points, defense_points=91 - points, bouts=bouts, target=target,
                delta=delta, won=delta >= 0, base=base, petit=petit, slam=slam,
                unit=unit, scores=scores, multiplier=multiplier)


def settle(state):
    taker = state['taker']
    captured = state['captured']
    contract = state.get('contract') or 'prise'
    reserve = state['discard'] if CONTRACTS[contract]['exchange'] else state['kitty']
    reserve_owner = state['tricks'][-1]['winner'] if contract == 'jiedi' else None if contract == 'contre' else taker
    taker_cards = captured[0] + (reserve if reserve_owner == taker else [])
    points = (sum(points2(c) for c in taker_cards) + state['adjust2'][0]) / 2
    bouts = len(BOUTS.intersection(taker_cards))
    tricks = state['tricks']
    teams = [0 if t['winner'] == taker else 1 for t in tricks]
    slam_team = teams[0] if len(teams) == 18 and len(set(teams)) == 1 else None
    slam = 200 if slam_team == 0 else -200 if slam_team == 1 else 0
    last = tricks[-1]
    if slam_team is not None and last.get('fool_slam'):
        last = tricks[-2]
    petit = 0
    if any(p['card'] == 'T1' for p in last['plays']):
        petit = 10 if last['winner'] == taker else -10
    result = score_numbers(points, bouts, taker, petit, slam, CONTRACTS[contract]['multiplier'])
    result.update(contract=contract, contract_name=CONTRACTS[contract]['name'], reserve_owner=reserve_owner,
                  reserve_points=sum(points2(c) for c in reserve) / 2,
                  reserve_team='庄家' if reserve_owner == taker else '防守方')
    result['excuse_adjustment'] = state['adjust2'][0] / 2
    result['excuse_note'] = state['excuse_note']
    result['tricks_won'] = teams.count(0)
    return result
