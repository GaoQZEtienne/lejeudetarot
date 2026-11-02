"""Seat-independent bot interface. Policies receive only a private player view.

This module holds the shared interface and random integration baseline.
Keep policies synchronous and bounded; Room runs them outside its event-loop lock.
"""
from copy import deepcopy
from dataclasses import dataclass, field
import random
from typing import Protocol

from .rules import auto_discard


@dataclass(frozen=True)
class BotDecision:
    action: str
    payload: dict = field(default_factory=dict)
    diagnostics: dict = field(default_factory=dict)


class BotPolicy(Protocol):
    def choose(self, observation: dict) -> BotDecision:
        """Return a bid/discard/play action using only the supplied observation."""
        ...


def observation_from_view(view):
    # Explicit allowlist: never pass Game.data, credentials, sockets or another
    # seat's private view to a policy. Copies also isolate policy mutations.
    keys = ('seat', 'phase', 'round', 'turn', 'hand', 'legal', 'players', 'taker',
            'first', 'passed', 'kitty', 'discard', 'exposed_trumps', 'trick',
            'tricks', 'contract', 'contracts', 'bids', 'kitty_revealed')
    observation = deepcopy({key: view[key] for key in keys})
    highest = view['contracts'].get(view['contract'], {}).get('multiplier', 0)
    observation['legal_bids'] = ['pass'] + [key for key, contract in view['contracts'].items()
                                           if contract['multiplier'] > highest]
    observation['ruleset'] = 'house-tarot-v1'
    return observation


class RandomPolicy:
    """Legal placeholder: coin-flip Prise/pass, legal discard, random legal play."""
    def __init__(self, rng=None):
        self.rng = rng or random.Random()

    def choose(self, observation):
        phase = observation['phase']
        if phase == 'bidding':
            bid = 'prise' if 'prise' in observation['legal_bids'] and self.rng.random() < .5 else 'pass'
            return BotDecision('bid', {'bid': bid})
        if phase == 'discard':
            return BotDecision('discard', {'cards': auto_discard(observation['hand'])})
        if phase == 'playing':
            return BotDecision('play', {'card': self.rng.choice(observation['legal'])})
        raise ValueError('Bot cannot act in this phase')
