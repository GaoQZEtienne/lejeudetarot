"""Deterministic deals and information-limited bot match helpers."""
import random

from game.bot_search import simulation_view
from game.cards import deal, deck, petit_sec
from game.state import Game


def fixed_game(seed, contract='prise'):
    rng = random.Random(seed)
    while True:
        cards = deck()
        rng.shuffle(cards)
        hands, kitty = deal(cards)
        if not any(petit_sec(h) for h in hands):
            break
    game = Game()
    game.data.update(phase='bidding', round=1, turn=seed % 4, first=(seed * 3) % 4,
                     hands=hands, kitty=kitty)
    if contract:
        game.action(seed % 4, 'bid', {'bid': contract})
        while game.data['phase'] == 'bidding':
            game.action(game.data['turn'], 'bid', {'bid': 'pass'})
    return game


def play_match(game, policies, diagnostics=None):
    while game.data['phase'] != 'finished':
        state = game.data
        if state['turn'] is None:
            game.timeout()
            continue
        seat = state['turn']
        decision = policies[seat].choose(simulation_view(game, seat))
        if diagnostics is not None and decision.diagnostics:
            diagnostics.append(decision.diagnostics)
        game.action(seat, decision.action, decision.payload)
    return game.data['result']
