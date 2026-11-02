"""Deterministic, imperfect-information tarot heuristics; no shared private memory."""
from heapq import nlargest
from math import comb

from .bot_policy import BotDecision
from .cards import BOUTS, SUITS, deck, points2, rank
from .contracts import CONTRACTS
from .rules import discard_options, lead_suit, trick_winner

CARDS = tuple(deck())
RANK = {c: rank(c) for c in CARDS}
VALUE = {c: points2(c) / 2 for c in CARDS}
BID_THRESHOLDS = {'prise': 10.8, 'garde': 13.2, 'jiedi': 15.6, 'sans': 17.5, 'contre': 21.5}


def same_team(a, b, taker):
    return (a == taker) == (b == taker)


class Knowledge:
    """Only deductions licensed by the house follow/cut rules.

    A Fool reveals no void. Undertrumping reveals no upper bound. Public kitty
    cards that are still unplayed belong to the taker after the exchange.
    """
    def __init__(self, observation):
        self.voids = [set() for _ in range(4)]
        self.played = set()
        self.known = [set() for _ in range(4)]
        for plays in [t['plays'] for t in observation['tricks']] + [observation['trick']]:
            lead = None
            for play in plays:
                card, seat = play['card'], play['seat']
                self.played.add(card)
                if card == 'F':
                    continue
                suit = card[0]
                if lead is None:
                    lead = suit
                elif suit != lead:
                    self.voids[seat].add(lead)
                    if lead != 'T' and suit != 'T':
                        self.voids[seat].add('T')
        self.known[observation['seat']] = set(observation['hand'])
        exchange = CONTRACTS.get(observation['contract'], {}).get('exchange', False)
        if exchange and observation['phase'] in ('playing', 'collecting', 'revealing', 'finished'):
            self.known[observation['taker']].update(set(observation['kitty']) - self.played)
        reserve = set(observation['discard']) | set(observation['exposed_trumps'])
        if observation['phase'] == 'discard':
            reserve.update(observation['kitty'])
        self.unknown = set(CARDS) - self.played - reserve - set().union(*self.known)
        self.pools = []
        self.draws = []
        for seat, player in enumerate(observation['players']):
            self.pools.append({c for c in self.unknown if c[0] not in self.voids[seat]})
            self.draws.append(max(0, player['count'] - len(self.known[seat])))

    def has_probability(self, seat, cards):
        if self.known[seat].intersection(cards):
            return 1.0
        pool = self.pools[seat]
        good = len(pool.intersection(cards))
        draws = min(len(pool), self.draws[seat])
        if not good or not draws:
            return 0.0
        if len(pool) - good < draws:
            return 1.0
        return 1 - comb(len(pool) - good, draws) / comb(len(pool), draws)


SUIT_CARDS = {s: {c for c in CARDS if c[0] == s} for s in (*SUITS, 'T')}


def hand_strength(hand):
    trumps = [RANK[c] for c in hand if c[0] == 'T']
    strength = .78 * len(trumps) + 1.9 * len(BOUTS.intersection(hand))
    strength += sum((r - 14) / 6 for r in trumps if r >= 15)
    for suit in SUITS:
        ranks = {RANK[c] for c in hand if c[0] == suit}
        strength += 1.35 * (14 in ranks) + .55 * (13 in ranks) + .2 * (12 in ranks)
        strength += .25 * max(0, len(ranks) - 4)
        strength += .55 * (not ranks and len(trumps) >= 4)
        strength += .5 * ({13, 14} <= ranks)
    if 'T1' in hand:
        strength += .8 if len(trumps) >= 6 else -2.5 if len(trumps) <= 2 else -.5
    return strength


def end_control(hand):
    """Rough last-trick control, used specifically for Jiedi's reserve."""
    trumps = [RANK[c] for c in hand if c[0] == 'T']
    return min(.85, .12 + .035 * len(trumps) + .035 * sum(r >= 18 for r in trumps)
               + .04 * sum(c.endswith('14') for c in hand if c[0] in SUITS))


def discard_score(hand, kitty, selected, contract):
    final = [c for c in hand if c not in selected] + list(kitty)
    trumps = [c for c in final if c[0] == 'T']
    reserve_factor = 2 * end_control(final) - 1 if contract == 'jiedi' else 1
    score = reserve_factor * sum(VALUE[c] for c in selected)
    score += sum(.09 * RANK[c] + (7 if c in BOUTS else 0) for c in trumps)
    for suit in SUITS:
        ranks = {RANK[c] for c in final if c[0] == suit}
        length = len(ranks)
        if not ranks:
            score += min(4, .65 * len(trumps))
            continue
        score += .22 * max(0, length - 3) ** 1.25
        for r in ranks:
            if r < 11:
                continue
            probability = {11: .12, 12: .2, 13: .38, 14: .88}[r]
            probability += .23 * (r + 1 in ranks) + .1 * (length >= 4)
            score += VALUE[f'{suit}{r}'] * min(.98, probability)
        if length == 1 and 14 not in ranks:
            score -= .5 * max(VALUE[f'{suit}{r}'] for r in ranks)
    return score


def ranked_discards(observation, limit=1, width=24):
    """Beam search of legal original-hand combinations; never discard the kitty."""
    hand, kitty = observation['hand'], observation['kitty']
    plain, trumps = discard_options(hand)
    quota = max(0, 6 - len(plain))
    pool = plain + trumps
    beam = [frozenset()]
    for size in range(1, 7):
        choices = set()
        for selected in beam:
            for card in pool:
                if card in selected or sum(c[0] == 'T' for c in selected) + (card[0] == 'T') > quota:
                    continue
                choices.add(selected | {card})
        beam = nlargest(width, choices, key=lambda selected: (
            discard_score(hand, kitty, selected, observation['contract']), tuple(sorted(selected))))
    return [sorted(cards) for cards in beam[:limit]]


def beat_probability(knowledge, seat, lead, winning):
    if winning is None or lead is None:
        return .5
    suit_cards = SUIT_CARDS[lead]
    follow = knowledge.has_probability(seat, suit_cards)
    if winning[0] == 'T':
        bigger = {c for c in SUIT_CARDS['T'] if RANK[c] > RANK[winning]}
        return knowledge.has_probability(seat, bigger) * (1 if lead == 'T' else 1 - follow)
    bigger = {c for c in suit_cards if RANK[c] > RANK[winning]}
    return min(1, knowledge.has_probability(seat, bigger) +
               (1 - follow) * knowledge.has_probability(seat, SUIT_CARDS['T']))


def play_scores(observation, knowledge=None):
    seat, taker = observation['seat'], observation['taker']
    hand, trick = observation['hand'], observation['trick']
    knowledge = knowledge or Knowledge(observation)
    remaining_trumps = (SUIT_CARDS['T'] - knowledge.played - set(hand)
                        - set(observation['exposed_trumps']) - set(observation['discard']))
    table_value = sum(VALUE[p['card']] + (10 if p['card'] == 'T1' else 0)
                      for p in trick if p['card'] != 'F')
    trump_count = sum(c[0] == 'T' for c in hand)
    scores = {}
    for card in observation['legal']:
        plays = trick + [dict(seat=seat, card=card)]
        lead = lead_suit(plays)
        winner = trick_winner(plays) if lead is not None else None
        winning = next((p['card'] for p in plays if p['seat'] == winner), None)
        chance = float(winner is not None and same_team(winner, seat, taker))
        for step in range(1, 5 - len(plays)):
            other = (seat + step) % 4
            if not observation['players'][other]['count']:
                continue
            can_beat = beat_probability(knowledge, other, lead, winning)
            if same_team(other, seat, taker):
                chance += (1 - chance) * can_beat * .8
            else:
                chance *= 1 - can_beat
        value = 0 if card == 'F' else VALUE[card] + (10 if card == 'T1' else 0)
        score = (2 * chance - 1) * (table_value + value)
        if card == 'F':
            score -= (1 - chance) * .5 + .1 * len(hand)
            if not trick:
                score -= 1.5
        elif card[0] == 'T':
            score -= .045 * RANK[card] + .07 * max(0, RANK[card] - 14)
            if len(hand) > 1 and RANK[card] >= 15:
                # T21's points/Bout are already safe in our hand. Spending it
                # over an unbeatable teammate must not earn a fictitious bonus.
                score -= VALUE[card] * (.98 if card == 'T21' else .65)
            if remaining_trumps and all(RANK[card] > RANK[c] for c in remaining_trumps):
                score -= .9 * (len(hand) > 2)
        elif RANK[card] >= 11:
            score -= .18 * (RANK[card] - 10)
        if not trick and card != 'F':
            length = sum(c[0] == card[0] for c in hand)
            if seat == taker:
                score += .2 * length if card[0] != 'T' else .45 * max(0, trump_count - 4)
            elif card[0] == 'T':
                score -= 1.2
            else:
                allies_cut = any(card[0] in knowledge.voids[i] and 'T' not in knowledge.voids[i]
                                 for i in range(4) if i != seat and same_team(i, seat, taker))
                score += 1.4 * allies_cut
        if observation['contract'] == 'jiedi' and len(hand) <= 3:
            reserve = sum(VALUE[c] for c in observation['discard']) if observation['discard'] else 10
            if len(hand) == 1:
                score += (2 * chance - 1) * reserve
            elif card[0] == 'T':
                score -= .06 * reserve * RANK[card] / 21
        scores[card] = score
    return scores


class NormalPolicy:
    def choose(self, observation):
        phase = observation['phase']
        if phase == 'bidding':
            strength = hand_strength(observation['hand'])
            choices = [bid for bid in observation['legal_bids'] if bid != 'pass'
                       and strength >= BID_THRESHOLDS[bid]]
            return BotDecision('bid', {'bid': choices[-1] if choices else 'pass'})
        if phase == 'discard':
            return BotDecision('discard', {'cards': ranked_discards(observation)[0]})
        if phase == 'playing':
            scores = play_scores(observation)
            card = max(scores, key=lambda c: (scores[c], -VALUE[c], -RANK[c], c))
            return BotDecision('play', {'card': card})
        raise ValueError('Bot cannot act in this phase')
