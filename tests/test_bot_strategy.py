from copy import deepcopy
import random
import time
import unittest
from unittest.mock import patch

from game.bot_policy import BotDecision
from game.bot_search import AdvancedPolicy, SamplingError, rollout, sample_world, simulation_view
from game.bot_strategy import Knowledge, NormalPolicy, hand_strength, ranked_discards
from game.cards import BOUTS, deck
from game.rules import legal_plays, validate_discard
from game.state import Game
from tests.bot_fixtures import fixed_game, play_match


def scenario(hand, trick=(), taker=0, contract='prise'):
    seat = len(trick) % 4
    return dict(seat=seat, turn=seat, phase='playing', round=1, first=0, hand=list(hand),
                legal=legal_plays(hand, list(trick)), trick=list(trick), tricks=[],
                players=[dict(count=len(hand)) for _ in range(4)],
                taker=taker, contract=contract, contracts={}, bids=[None] * 4, passed=[],
                kitty=[], discard=[], exposed_trumps=[], kitty_revealed=True,
                legal_bids=['pass', 'prise', 'garde', 'jiedi', 'sans', 'contre'])


class NormalStrategyTests(unittest.TestCase):
    def test_weak_hand_passes_strong_hand_calls_and_only_raises_legally(self):
        policy = NormalPolicy()
        weak = [f'{s}{r}' for s in ('S', 'H', 'D') for r in range(1, 7)]
        strong = [f'T{r}' for r in (1, 21, 20, 19, 18, 17, 16, 15, 14, 13)] + ['S14', 'H14', 'D14', 'C14', 'F', 'S13', 'H13', 'D13']
        view = scenario(weak)
        view['phase'] = 'bidding'
        self.assertEqual(policy.choose(view).payload['bid'], 'pass')
        view['hand'] = strong
        self.assertGreater(hand_strength(strong), hand_strength(weak))
        self.assertEqual(policy.choose(view).payload['bid'], 'contre')
        view['legal_bids'] = ['pass']
        self.assertEqual(policy.choose(view).payload['bid'], 'pass')

    def test_cheapest_winning_trump_and_do_not_overtrump_safe_teammate(self):
        trick = [dict(seat=0, card='T4'), dict(seat=1, card='T2'), dict(seat=2, card='T3')]
        view = scenario(['T5', 'T21'], trick, taker=0)
        self.assertEqual(NormalPolicy().choose(view).payload['card'], 'T5')
        view = scenario(['T2', 'T21'], [dict(seat=0, card='T20'), dict(seat=1, card='T3'), dict(seat=2, card='T4')], taker=1)
        self.assertEqual(NormalPolicy().choose(view).payload['card'], 'T2')

    def test_feed_points_to_safe_teammate_and_save_fool_for_expensive_loss(self):
        trick = [dict(seat=0, card='S14'), dict(seat=1, card='S2'), dict(seat=2, card='S3')]
        view = scenario(['S1', 'S13'], trick, taker=1)
        self.assertEqual(NormalPolicy().choose(view).payload['card'], 'S13')
        view = scenario(['S13', 'F'], trick, taker=0)
        self.assertEqual(NormalPolicy().choose(view).payload['card'], 'F')
        view = scenario(['T1', 'T3'], [dict(seat=0, card='T20'), dict(seat=1, card='T4'), dict(seat=2, card='T5')], taker=0)
        self.assertEqual(NormalPolicy().choose(view).payload['card'], 'T3')

    def test_fool_does_not_prove_void_undertrump_does_not_prove_no_high_trump(self):
        view = scenario(['S1', 'H1'])
        view['tricks'] = [dict(plays=[dict(seat=0, card='S4'), dict(seat=1, card='F'),
                                           dict(seat=2, card='T3'), dict(seat=3, card='H2')])]
        knowledge = Knowledge(view)
        self.assertEqual(knowledge.voids[1], set())
        self.assertEqual(knowledge.voids[2], {'S'})
        self.assertEqual(knowledge.voids[3], {'S', 'T'})
        self.assertIn('T21', knowledge.pools[2])
        view['tricks'] = [dict(plays=[dict(seat=0, card='F'), dict(seat=1, card='D5'),
                                           dict(seat=2, card='T4'), dict(seat=3, card='D6')])]
        self.assertEqual(Knowledge(view).voids[0], set())
        self.assertEqual(Knowledge(view).voids[2], {'D'})

    def test_discard_combinations_are_legal_original_hand_and_account_for_kitty(self):
        for seed in range(20):
            for contract in ('prise', 'jiedi'):
                game = fixed_game(seed, contract)
                view = simulation_view(game, game.data['turn'])
                for cards in ranked_discards(view, limit=4):
                    validate_discard(view['hand'], cards)
                    self.assertFalse(set(cards).intersection(view['kitty']))
                    self.assertFalse(set(cards).intersection(BOUTS))
        # All plain cards are mandatory when there are fewer than six.
        hand = [f'T{i}' for i in range(1, 15)] + ['S14', 'S2', 'H3', 'F']
        view = scenario(hand)
        view.update(phase='discard', kitty=['C1', 'C2', 'C3', 'D1', 'D2', 'D3'])
        cards = NormalPolicy().choose(view).payload['cards']
        validate_discard(hand, cards)
        self.assertTrue({'S2', 'H3'} <= set(cards))

    def test_normal_completes_every_contract_and_does_not_mutate_observation(self):
        for contract in ('prise', 'garde', 'jiedi', 'sans', 'contre'):
            game = fixed_game(17, contract)
            view = simulation_view(game, game.data['turn'])
            before = deepcopy(view)
            NormalPolicy().choose(view)
            self.assertEqual(view, before)
            result = play_match(game, [NormalPolicy()] * 4)
            self.assertEqual(len(game.data['tricks']), 18)
            self.assertEqual(sum(result['scores']), 0)
            self.assertEqual(result['points'] + result['defense_points'], 91)


class SearchStrategyTests(unittest.TestCase):
    def test_samples_conserve_cards_match_voids_kitty_and_replay_full_history(self):
        normal = NormalPolicy()
        checked = 0
        for contract in ('prise', 'garde', 'jiedi', 'sans', 'contre'):
            game = fixed_game(71, contract)
            while game.data['phase'] != 'finished':
                s = game.data
                if s['turn'] is None:
                    game.timeout()
                    continue
                view = simulation_view(game, s['turn'])
                if s['phase'] == 'playing' and len(s['tricks']) in (0, 5, 12, 17):
                    for seed in range(3):
                        world = sample_world(view, random.Random(seed))
                        w = world.data
                        history = [t['plays'] for t in w['tricks']] + [w['trick']]
                        played = [p['card'] for plays in history for p in plays]
                        reserve = w['discard'] if contract in ('prise', 'garde', 'jiedi') else w['kitty']
                        cards = sum(w['hands'], []) + reserve + played
                        self.assertEqual(sorted(cards), sorted(deck()))
                        self.assertEqual(w['hands'][view['seat']], sorted(view['hand']))
                        self.assertEqual(w['captured'], s['captured'])
                        self.assertEqual(w['adjust2'], s['adjust2'])
                        initial = deepcopy(w['hands'])
                        for plays in history:
                            for play in plays:
                                initial[play['seat']].append(play['card'])
                        for plays in history:
                            partial = []
                            for play in plays:
                                self.assertIn(play['card'], legal_plays(initial[play['seat']], partial))
                                initial[play['seat']].remove(play['card'])
                                partial.append(play)
                        self.assertEqual([set(h) for h in initial], [set(h) for h in w['hands']])
                        if contract in ('prise', 'garde', 'jiedi'):
                            self.assertFalse(set(w['discard']).intersection(view['kitty']))
                            self.assertFalse(set(w['discard']).intersection(BOUTS))
                            self.assertEqual({c for c in w['discard'] if c[0] == 'T'}, set(view['exposed_trumps']))
                            original = [c for c in initial[w['taker']] if c not in w['kitty']]
                            original += [p['card'] for plays in history for p in plays
                                         if p['seat'] == w['taker'] and p['card'] not in w['kitty']]
                            validate_discard(original + w['discard'], w['discard'])
                        checked += 1
                action = normal.choose(view)
                game.action(s['turn'], action.action, action.payload)
        self.assertGreaterEqual(checked, 100)

    def test_exposed_necessary_trumps_constrain_original_hand_as_well(self):
        game = Game()
        original = [f'T{i}' for i in range(1, 16)] + ['S14', 'H2', 'F']
        kitty = ['D1', 'D2', 'D3', 'C1', 'C2', 'C3']
        rest = [c for c in deck() if c not in original + kitty]
        game.data.update(phase='discard', round=1, turn=0, first=0, taker=0,
                         contract='prise', kitty=kitty, kitty_revealed=True,
                         hands=[original, rest[:18], rest[18:36], rest[36:]])
        game.action(0, 'discard', {'cards': ['H2', 'T2', 'T3', 'T4', 'T5', 'T6']})
        game.action(0, 'play', {'card': 'S14'})
        view = simulation_view(game, 1)
        for seed in range(10):
            world = sample_world(view, random.Random(seed))
            original = [c for c in world.data['hands'][0] + ['S14'] if c not in kitty]
            validate_discard(original + world.data['discard'], world.data['discard'])

    def test_sampling_bidding_and_discard_and_inconsistent_observation_fails_closed(self):
        for contract in (None, 'prise'):
            game = fixed_game(5, contract)
            view = simulation_view(game, game.data['turn'])
            world = sample_world(view, random.Random(0))
            self.assertEqual(sorted(sum(world.data['hands'], []) + world.data['kitty']), sorted(deck()))
            self.assertEqual(world.data['hands'][view['seat']], sorted(view['hand']))
        view['players'][1]['count'] += 1
        with self.assertRaises(SamplingError):
            sample_world(view, random.Random(0))

    def test_search_budget_and_zero_budget_fallback(self):
        game = fixed_game(31, 'sans')
        view = simulation_view(game, game.data['turn'])
        baseline = NormalPolicy().choose(view)
        fallback = AdvancedPolicy(budget=0).choose(view)
        self.assertEqual((fallback.action, fallback.payload), (baseline.action, baseline.payload))
        self.assertEqual(fallback.diagnostics['samples'], 0)
        start = time.perf_counter()
        choice = AdvancedPolicy(budget=.04, seed=1).choose(view)
        self.assertLess(time.perf_counter() - start, .2)
        self.assertIn(choice.payload['card'], view['legal'])

    def test_advanced_bidding_and_discard_use_legal_bounded_search(self):
        auction = next(fixed_game(seed, None) for seed in range(80)
                       if hand_strength(fixed_game(seed, None).data['hands'][seed % 4]) > 14)
        cases = [auction, fixed_game(3, 'prise'), fixed_game(3, 'jiedi')]
        for game in cases:
            seat = game.data['turn']
            view = simulation_view(game, seat)
            before = deepcopy(view)
            choice = AdvancedPolicy(budget=.2, max_samples=3, seed=1).choose(view)
            self.assertEqual(view, before)
            self.assertGreaterEqual(choice.diagnostics['samples'], 2)
            game.action(seat, choice.action, choice.payload)

    def test_search_uses_same_samples_and_selects_better_contract_score(self):
        game = fixed_game(31, 'sans')
        view = simulation_view(game, game.data['turn'])
        advanced = AdvancedPolicy(budget=1, max_samples=4, seed=1)
        baseline = NormalPolicy().choose(view)
        alternate = next(c for c in view['legal'] if c != baseline.payload['card'])
        calls = []

        def score(world, decision, seat, deadline):
            calls.append((id(world), decision.payload['card']))
            return 50 if decision.payload['card'] == alternate else -50

        choices = [baseline, BotDecision('play', {'card': alternate})]
        with patch.object(advanced, 'candidates', return_value=choices), patch('game.bot_search.rollout', side_effect=score):
            result = advanced.choose(view)
        self.assertEqual(result.payload['card'], alternate)
        self.assertEqual(result.diagnostics['samples'], 4)
        self.assertEqual(len(calls), 8)
        self.assertTrue(all(calls[i][0] == calls[i + 1][0] for i in range(0, 8, 2)))
        # Changing names/totals cannot change a seeded, fixed-sample decision.
        view2 = deepcopy(view)
        for player in view2['players']:
            player.update(name='other', total=99999, kind='bot')
        advanced = AdvancedPolicy(budget=1, max_samples=2, seed=1)
        a, b = advanced.choose(view), advanced.choose(view2)
        self.assertEqual((a.action, a.payload), (b.action, b.payload))

    def test_rollout_uses_actual_fool_and_jiedi_scoring(self):
        for contract in ('jiedi', 'sans', 'contre'):
            game = fixed_game(19, contract)
            normal = NormalPolicy()
            while len(game.data['tricks']) < 15 or game.data['turn'] is None:
                s = game.data
                if s['turn'] is None:
                    game.timeout()
                else:
                    decision = normal.choose(simulation_view(game, s['turn']))
                    game.action(s['turn'], decision.action, decision.payload)
            seat = game.data['turn']
            decision = normal.choose(simulation_view(game, seat))
            score = rollout(game, decision, seat, time.perf_counter() + 1)
            copy = Game(game.data)
            copy.action(seat, decision.action, decision.payload)
            actual = play_match(copy, [normal] * 4)
            self.assertEqual(score, actual['scores'][seat])


if __name__ == '__main__':
    unittest.main()
