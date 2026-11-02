"""Bounded hidden-card sampling with paired, information-limited rollouts.

This is root sampling, not a full information-set MCTS or an exact posterior.
Every simulated player sees only its own simulated hand and public history.
"""
from copy import deepcopy
import math
import random
import time

from .bot_policy import BotDecision
from .bot_strategy import (BID_THRESHOLDS, CARDS, Knowledge, NormalPolicy, RANK,
                           hand_strength, play_scores, ranked_discards)
from .cards import BOUTS, SUITS, petit_sec
from .contracts import CONTRACTS
from .rules import legal_plays
from .state import Game


class SamplingError(ValueError):
    pass


def sample_world(observation, rng):
    """Assign all 78 cards without relaxing any public or private constraints."""
    phase, own = observation['phase'], observation['seat']
    knowledge = Knowledge(observation)
    exchange = CONTRACTS.get(observation['contract'], {}).get('exchange', False)
    after_exchange = exchange and phase == 'playing'
    hands = [set() for _ in range(4)]
    hands[own].update(observation['hand'])
    if after_exchange:
        hands[observation['taker']].update(set(observation['kitty']) - knowledge.played)
    if phase == 'discard':
        reserve = set(observation['kitty'])
    elif after_exchange:
        reserve = set(observation['discard']) | set(observation['exposed_trumps'])
    else:
        reserve = set()
    fixed = reserve | knowledge.played
    for hand in hands:
        if fixed.intersection(hand):
            raise SamplingError('A known card has two owners')
        fixed.update(hand)
    cards = sorted(set(CARDS) - fixed)
    capacity = [p['count'] - len(hands[i]) for i, p in enumerate(observation['players'])] + [6 - len(reserve)]
    if any(n < 0 for n in capacity) or sum(capacity) != len(cards):
        raise SamplingError('Known card counts are inconsistent')
    allowed = {}
    public_kitty = set(observation['kitty'])
    for card in cards:
        groups = [i for i in range(4) if i != own and capacity[i] and card[0] not in knowledge.voids[i]]
        # Unexposed trumps, kings, Bouts and original kitty cards cannot be
        # hidden in the taker's discard under the house discard rules.
        discardable = card[0] in SUITS and RANK[card] != 14 and card not in public_kitty
        if after_exchange and observation['exposed_trumps'] and discardable:
            # Necessary exposed trumps prove ALL original discardable plain
            # cards were discarded; only original kitty plain cards can remain.
            groups = [i for i in groups if i != observation['taker']]
        if capacity[4] and (not after_exchange or discardable):
            groups.append(4)
        rng.shuffle(groups)
        allowed[card] = groups
    rng.shuffle(cards)
    cards.sort(key=lambda c: len(allowed[c]))
    assigned = [[] for _ in range(5)]

    def place(card, visited):
        for group in allowed[card]:
            if group in visited:
                continue
            visited.add(group)
            if len(assigned[group]) < capacity[group]:
                assigned[group].append(card)
                return True
            residents = list(assigned[group])
            rng.shuffle(residents)
            for previous in residents:
                if place(previous, visited):
                    assigned[group].remove(previous)
                    assigned[group].append(card)
                    return True
        return False

    for card in cards:
        if not place(card, set()):
            raise SamplingError('No assignment satisfies the observed history')
    hands = [sorted(hand | set(assigned[i])) for i, hand in enumerate(hands)]
    reserve = sorted(reserve | set(assigned[4]))
    if phase in ('bidding', 'discard') and any(petit_sec(hand) for hand in hands):
        raise SamplingError('Petit sec hands were excluded by the real deal')

    game = Game()
    state = game.data
    state.update(phase=phase, round=observation['round'], turn=observation['turn'],
                 first=observation['first'], taker=observation['taker'],
                 contract=observation['contract'], hands=hands,
                 kitty=list(observation['kitty']) if after_exchange else reserve,
                 discard=reserve if after_exchange else [],
                 passed=list(observation['passed']), bids=list(observation['bids']),
                 called=[bid is not None for bid in observation['bids']],
                 kitty_revealed=observation['kitty_revealed'],
                 exposed_trumps=list(observation['exposed_trumps']))
    # Reuse the actual engine for Fool ownership/compensation and team captures.
    for trick in observation['tricks']:
        state.update(phase='collecting', trick=deepcopy(trick['plays']))
        game.collect()
    state.update(phase=phase, turn=observation['turn'], trick=deepcopy(observation['trick']))
    return game


def simulation_view(game, seat):
    """Fresh policy view; deliberately excludes all other sampled private hands."""
    s = game.data
    highest = CONTRACTS.get(s['contract'], {}).get('multiplier', 0)
    return dict(seat=seat, phase=s['phase'], round=s['round'], turn=s['turn'],
                hand=list(s['hands'][seat]),
                legal=legal_plays(s['hands'][seat], s['trick']) if s['phase'] == 'playing' else [],
                players=[dict(count=len(h)) for h in s['hands']],
                first=s['first'], taker=s['taker'], passed=list(s['passed']),
                kitty=list(s['kitty']) if s['kitty_revealed'] else [],
                discard=list(s['discard']) if seat == s['taker'] else [],
                exposed_trumps=list(s['exposed_trumps']), trick=s['trick'], tricks=s['tricks'],
                contract=s['contract'], contracts=CONTRACTS, bids=list(s['bids']),
                kitty_revealed=s['kitty_revealed'], ruleset='house-tarot-v1',
                legal_bids=['pass'] + [c for c in CONTRACTS if CONTRACTS[c]['multiplier'] > highest])


def rollout(world, decision, seat, deadline):
    game = Game(world.data)
    original_round = game.data['round']
    game.action(seat, decision.action, decision.payload)
    normal = NormalPolicy()
    while game.data['phase'] != 'finished':
        if time.perf_counter() >= deadline:
            return None
        if game.data['round'] != original_round:
            return 0.0  # All passed: this deal contributes no score.
        s = game.data
        if s['turn'] is None:
            game.timeout()
            continue
        view = simulation_view(game, s['turn'])
        if s['phase'] == 'discard':
            action = BotDecision('discard', {'cards': ranked_discards(view, width=3)[0]})
        else:
            action = normal.choose(view)
        game.action(s['turn'], action.action, action.payload)
    return game.data['result']['scores'][seat]


class AdvancedPolicy:
    def __init__(self, budget=.6, max_samples=24, seed=None):
        self.budget = budget
        self.max_samples = max_samples
        self.seed = seed

    def candidates(self, observation, baseline):
        phase = observation['phase']
        choices = [baseline]
        if phase == 'playing':
            scores = play_scores(observation)
            cards = sorted(scores, key=lambda c: (-scores[c], c))[:5]
            if 'F' in scores and 'F' not in cards:
                cards.append('F')
            choices += [BotDecision('play', {'card': c}) for c in cards]
        elif phase == 'discard':
            choices += [BotDecision('discard', {'cards': cards}) for cards in ranked_discards(observation, limit=4)]
        elif phase == 'bidding':
            strength = hand_strength(observation['hand'])
            choices.append(BotDecision('bid', {'bid': 'pass'}))
            choices += [BotDecision('bid', {'bid': bid}) for bid in observation['legal_bids']
                        if bid != 'pass' and strength >= BID_THRESHOLDS[bid] - 1]
        unique = []
        for choice in choices:
            if choice not in unique:
                unique.append(choice)
        return unique

    def choose(self, observation):
        start = time.perf_counter()
        deadline = start + max(0, self.budget)
        baseline = NormalPolicy().choose(observation)
        if self.budget <= 0:
            return BotDecision(baseline.action, baseline.payload, dict(samples=0, fallback=True))
        choices = self.candidates(observation, baseline)
        if len(choices) == 1:
            return BotDecision(baseline.action, baseline.payload, dict(samples=0, fallback=True))
        rng = random.Random(self.seed)
        differences = [[] for _ in choices]
        samples, attempts, evaluations = 0, 0, 0
        while samples < self.max_samples and time.perf_counter() < deadline:
            attempts += 1
            if attempts > self.max_samples * 4 + 8:
                break
            try:
                world = sample_world(observation, rng)
            except SamplingError:
                continue  # Never relax a void or peek at the real state.
            values = []
            # The exact same hidden world is used for ALL choices. Discard a
            # partial batch at the deadline to avoid favouring faster actions.
            for choice in choices:
                value = rollout(world, choice, observation['seat'], deadline)
                evaluations += 1
                if value is None:
                    break
                values.append(value)
            if len(values) != len(choices):
                break
            samples += 1
            for index, value in enumerate(values):
                differences[index].append(value - values[0])
        selected, best = 0, 0.0
        if samples >= 2:
            for index, values in enumerate(differences[1:], 1):
                mean = sum(values) / samples
                variance = sum((v - mean) ** 2 for v in values) / (samples - 1)
                improvement = mean - .45 * math.sqrt(variance / samples)
                if improvement > best:
                    selected, best = index, improvement
        result = choices[selected]
        return BotDecision(result.action, result.payload,
                           dict(samples=samples, rollouts=evaluations, fallback=selected == 0,
                                elapsed_ms=round((time.perf_counter() - start) * 1000, 1)))
