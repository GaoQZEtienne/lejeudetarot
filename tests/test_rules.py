import unittest
from unittest.mock import patch

from game.cards import BOUTS, deal, deck, petit_sec, points2
from game.rules import RuleError, auto_discard, legal_plays, trick_winner, validate_discard
from game.scoring import score_numbers
from game.scoring import settle
from game.contracts import CONTRACTS
from game.state import Game


def plays(*cards):
    return [dict(seat=i, card=c) for i, c in enumerate(cards)]


class RulesTests(unittest.TestCase):
    def test_deck_and_deal_rounds(self):
        cards = deck()
        hands, kitty = deal(cards)
        self.assertEqual([len(h) for h in hands], [18] * 4)
        self.assertEqual(kitty, cards[12::13])
        self.assertEqual(kitty[-1], cards[-1])
        self.assertEqual(len(set(sum(hands, []) + kitty)), 78)
        self.assertEqual(sum(points2(c) for c in cards), 182)
        self.assertEqual([points2(c) for c in ('S14', 'S13', 'S12', 'S11', 'T5', 'F')], [9, 7, 5, 3, 1, 9])

    def test_petit_sec(self):
        self.assertTrue(petit_sec(['T1', 'S5']))
        self.assertFalse(petit_sec(['T1', 'F', 'S5']))
        self.assertFalse(petit_sec(['T1', 'T2']))

    def test_follow_cut_but_no_overtrump_requirement(self):
        hand = ['S2', 'T5', 'T15', 'F', 'H3']
        self.assertCountEqual(legal_plays(hand, plays('S14', 'T10')), ['S2', 'F'])
        self.assertCountEqual(legal_plays(hand, plays('D14', 'T10')), ['T5', 'T15', 'F'])
        self.assertCountEqual(legal_plays(hand, plays('T10')), ['T5', 'T15', 'F'])
        self.assertCountEqual(legal_plays(['S2', 'F'], plays('T10')), ['S2', 'F'])
        self.assertCountEqual(legal_plays(hand, plays('F')), hand)
        self.assertEqual(legal_plays(['S2', 'T5'], plays('F', 'S14')), ['S2'])

    def test_winner_ignores_fool_and_off_suit(self):
        self.assertEqual(trick_winner(plays('F', 'S10', 'H14', 'S11')), 3)
        self.assertEqual(trick_winner(plays('S14', 'T1', 'T5', 'F')), 2)

    def test_discard_forbidden_and_necessary_trumps(self):
        hand = ['S1', 'S2', 'S3', 'S4', 'S5', 'S6', 'S14', 'F', 'T1', 'T21', 'T2']
        for forbidden in ['S14', 'F', 'T1', 'T21', 'T2', 'H1']:
            with self.assertRaises(RuleError):
                validate_discard(hand, ['S1', 'S2', 'S3', 'S4', 'S5', forbidden])
        short = ['S1', 'S2', 'S14', 'T1', 'T21', 'F', 'T2', 'T3', 'T4', 'T5', 'T6']
        self.assertEqual(len(auto_discard(short)), 6)
        with self.assertRaises(RuleError):
            validate_discard(short, ['S1', 'T2', 'T3', 'T4', 'T5', 'T6'])
        for _ in range(10):
            choice = auto_discard(short, ['T3', 'S1'])
            self.assertTrue({'T3', 'S1', 'S2'} <= set(choice))

    def test_take_kitty_only_after_discard(self):
        game = Game()
        game.new_deal()
        seat = game.data['turn']
        original = list(game.data['hands'][seat])
        kitty = list(game.data['kitty'])
        game.action(seat, 'bid', {'bid': 'take'})
        while game.data['phase'] == 'bidding':
            game.timeout()
        self.assertEqual(game.data['hands'][seat], original)
        with self.assertRaises(RuleError):
            game.action(seat, 'discard', {'cards': kitty})
        discarded = auto_discard(original)
        game.action(seat, 'discard', {'cards': discarded})
        self.assertEqual(len(game.data['hands'][seat]), 18)
        self.assertTrue(set(kitty) <= set(game.data['hands'][seat]))
        self.assertFalse(set(discarded) & set(game.data['hands'][seat]))

    def test_all_pass_redeals_and_wrong_turn_rejected(self):
        game = Game()
        game.new_deal()
        with self.assertRaises(RuleError):
            game.action((game.data['turn'] + 1) % 4, 'bid', {'bid': 'take'})
        for _ in range(4):
            game.action(game.data['turn'], 'bid', {'bid': 'pass'})
        self.assertEqual(game.data['round'], 2)
        self.assertEqual(game.data['phase'], 'bidding')
        self.assertIn('四人均不叫', game.data['notice'])

    def make_trick(self, cards, past=0, past_winner=0, taker=0):
        game = Game()
        game.data.update(phase='collecting', taker=taker, trick=plays(*cards),
                         tricks=[{'winner': past_winner, 'plays': plays('S1', 'S2', 'S3', 'S4')}] * past)
        return game

    def test_fool_compensation_including_final_trick(self):
        game = self.make_trick(('F', 'S1', 'S14', 'S2'))
        game.collect()
        self.assertIn('F', game.data['captured'][0])
        self.assertEqual(game.data['adjust2'], [-1, 1])
        # The final-trick fool stays with its owner, including within defense.
        game = self.make_trick(('S1', 'F', 'S14', 'S2'), past=17, past_winner=0)
        game.collect()
        self.assertEqual(game.data['tricks'][-1]['winner'], 2)
        self.assertIn('F', game.data['captured'][1])
        self.assertEqual(game.data['adjust2'], [0, 0])
        game = self.make_trick(('F', 'S1', 'S14', 'S2'), past=17, past_winner=2)
        game.collect()
        self.assertIn('F', game.data['captured'][0])
        self.assertEqual(game.data['adjust2'], [-1, 1])

    def test_fool_slam_and_penultimate_petit(self):
        game = self.make_trick(('F', 'S14', 'S1', 'S2'), past=17, past_winner=0)
        game.data['tricks'][-1] = {'winner': 0, 'plays': plays('T1', 'H1', 'H2', 'H3')}
        game.collect()
        self.assertEqual(game.data['tricks'][-1]['winner'], 0)
        self.assertTrue(game.data['tricks'][-1]['fool_slam'])
        self.assertEqual(game.data['result']['slam'], 200)
        self.assertEqual(game.data['result']['petit'], 10)

    def test_score_examples_and_half_point(self):
        self.assertEqual(score_numbers(45, 2, 0, petit=10)['scores'], [117, -39, -39, -39])
        self.assertEqual(score_numbers(40, 2, 0, petit=-10)['scores'], [-108, 36, 36, 36])
        self.assertFalse(score_numbers(40.5, 2, 0)['won'])
        self.assertEqual(score_numbers(41, 2, 0)['delta'], 0)

    def test_complete_games_conserve_cards_and_points(self):
        for iteration in range(30):
            game = Game()
            game.new_deal()
            contract = list(CONTRACTS)[iteration % 5]
            game.action(game.data['turn'], 'bid', {'bid': contract})
            while game.data['phase'] == 'bidding':
                game.timeout()
            game.timeout()
            while game.data['phase'] != 'finished':
                game.timeout()
            s = game.data
            cards = sum(s['captured'], []) + (s['discard'] if CONTRACTS[contract]['exchange'] else s['kitty'])
            self.assertEqual(len(s['tricks']), 18)
            self.assertEqual(len(cards), 78)
            self.assertEqual(set(cards), set(deck()))
            self.assertEqual(sum(points2(c) for c in cards) + sum(s['adjust2']), 182)
            self.assertEqual(sum(s['result']['scores']), 0)
            self.assertEqual(sum(len(h) for h in s['hands']), 0)
            self.assertEqual(len(BOUTS & set(cards)), 3)
            self.assertEqual(s['result']['multiplier'], CONTRACTS[contract]['multiplier'])

    def test_repeated_bidding_and_maximum_contract(self):
        game = Game()
        game.new_deal()
        first = game.data['turn']
        for bid in ['prise', 'garde', 'pass', 'pass', 'jiedi', 'sans', 'contre']:
            if bid == 'jiedi':
                self.assertEqual(game.data['turn'], first)
                with self.assertRaises(RuleError):
                    game.action(first, 'bid', {'bid': 'garde'})
            game.action(game.data['turn'], 'bid', {'bid': bid})
        self.assertEqual(game.data['phase'], 'playing')
        self.assertEqual(game.data['taker'], first)
        self.assertEqual(game.data['contract'], 'contre')
        self.assertFalse(game.data['kitty_revealed'])

    def test_reserve_scoring_and_contract_multipliers(self):
        game = Game()
        game.new_deal()
        game.action(game.data['turn'], 'bid', {'bid': 'jiedi'})
        while game.data['phase'] != 'finished':
            game.timeout()
        s = game.data
        taker = s['taker']
        reserve = sum(points2(c) for c in s['discard']) / 2
        s['tricks'][-1]['winner'] = taker
        won_reserve = settle(s)
        s['tricks'][-1]['winner'] = (taker + 1) % 4
        lost_reserve = settle(s)
        self.assertEqual(won_reserve['points'] - lost_reserve['points'], reserve)
        self.assertEqual(lost_reserve['reserve_owner'], (taker + 1) % 4)
        s['contract'] = 'sans'
        sans = settle(s)
        s['contract'] = 'contre'
        contre = settle(s)
        self.assertEqual(sans['points'] - contre['points'], sum(points2(c) for c in s['kitty']) / 2)
        self.assertEqual(score_numbers(45, 2, 0, petit=10, multiplier=3)['unit'], 117)
        self.assertEqual(score_numbers(45, 2, 0, slam=200, multiplier=6)['unit'], 374)

    def test_achievement_counts_and_resets(self):
        game = Game()
        game.new_deal()
        for _ in range(20):
            game.timeout()
        self.assertEqual(game.data['round'], 6)
        self.assertEqual(game.data['announcements'][-1]['kind'], 'turtle')
        self.assertEqual(game.data['announcements'][-1]['seats'], [0, 1, 2, 3])
        self.assertTrue(all(stats['passes'] == 0 for stats in game.data['stats']))
        for _ in range(3):
            game.data['turn'] = 0
            game.action(0, 'bid', {'bid': 'prise'})
            while game.data['phase'] == 'bidding':
                game.timeout()
            game.new_deal()
        self.assertEqual(game.data['announcements'][-1]['kind'], 'bidder')
        self.assertEqual(game.data['stats'][0]['calls'], 0)
        s = game.data
        s.update(taker=0, result={'won': True})
        game.finish_awards()
        # A defense win keeps player 0's streak and does not reset taker wins.
        s.update(taker=1, result={'won': False})
        game.finish_awards()
        s.update(taker=0, result={'won': True})
        game.finish_awards()
        self.assertEqual([e['kind'] for e in s['announcements'][-2:]], ['streak', 'taker'])
        self.assertEqual(s['stats'][0]['wins'], 0)
        self.assertEqual(s['stats'][0]['taker_wins'], 0)
        game.finish_awards()
        s['result']['won'] = False
        game.finish_awards()
        self.assertEqual(s['stats'][0]['wins'], 0)
        self.assertEqual(s['stats'][0]['taker_wins'], 0)


if __name__ == '__main__':
    unittest.main()
