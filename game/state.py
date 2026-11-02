from copy import deepcopy
import secrets

from .cards import RNG, deal, deck, petit_sec, sort_hand
from .rules import RuleError, auto_discard, legal_plays, trick_winner, validate_discard
from .scoring import settle
from .contracts import CONTRACTS


class Game:
    def __init__(self, data=None):
        self.data = deepcopy(data) if data else dict(phase='lobby', round=0, turn=None,
            hands=[[], [], [], []], kitty=[], discard=[], trick=[], tricks=[], taker=None,
            first=None, passed=[], captured=[[], []], adjust2=[0, 0], draft=[],
            exposed_trumps=[], result=None, excuse_note='', notice='四位好友入座后，由房主开始。',
            contract=None, bids=[None] * 4, called=[False] * 4, kitty_revealed=False,
            stats=[dict(wins=0, taker_wins=0, calls=0, passes=0) for _ in range(4)],
            announcements=[], announcement_serial=0, award_epoch=secrets.token_hex(8),
            restart_poll=None, restart_cooldown=0)

    def announce(self, kind, seats):
        if seats:
            s = self.data
            s['announcement_serial'] += 1
            s['announcements'].append(dict(id=s['announcement_serial'], kind=kind, seats=seats))
            s['announcements'] = s['announcements'][-20:]

    def finish_bidding(self):
        s = self.data
        for i, stats in enumerate(s['stats']):
            called = s['called'][i]
            stats['calls'] = stats['calls'] + 1 if called else 0
            stats['passes'] = 0 if called else stats['passes'] + 1
        for key, threshold, kind in [('passes', 5, 'turtle'), ('calls', 3, 'bidder')]:
            seats = [i for i, stats in enumerate(s['stats']) if stats[key] >= threshold]
            self.announce(kind, seats)
            for i in seats:
                s['stats'][i][key] = 0
        if s['taker'] is None:
            self.new_deal('四人均不叫，重新发牌。')
        elif CONTRACTS[s['contract']]['exchange']:
            s.update(phase='discard', turn=s['taker'], kitty_revealed=True,
                     notice='底牌已公开；庄家请从原手牌中扣六张。')
        else:
            s.update(phase='playing', turn=s['first'], notice='不换底牌，底牌保持隐藏。开始第一墩。')

    def finish_awards(self):
        s = self.data
        for i, stats in enumerate(s['stats']):
            won = s['result']['won'] if i == s['taker'] else not s['result']['won']
            stats['wins'] = stats['wins'] + 1 if won else 0
            if i == s['taker']:
                stats['taker_wins'] = stats['taker_wins'] + 1 if won else 0
        for key, threshold, kind in [('wins', 3, 'streak'), ('taker_wins', 2, 'taker')]:
            seats = [i for i, stats in enumerate(s['stats']) if stats[key] >= threshold]
            self.announce(kind, seats)
            for i in seats:
                s['stats'][i][key] = 0

    def new_deal(self, reason=''):
        retained = {key: deepcopy(self.data[key]) for key in
                    ('stats', 'announcements', 'announcement_serial', 'award_epoch', 'restart_poll', 'restart_cooldown')}
        previous_round = self.data['round']
        rejected = 0
        for _ in range(100):
            cards = deck()
            RNG.shuffle(cards)
            hands, kitty = deal(cards)
            if not any(petit_sec(h) for h in hands):
                break
            rejected += 1
        else:
            raise RuntimeError('连续发牌失败，请检查随机源')
        first = RNG.randrange(4)
        self.data = dict(phase='bidding', round=previous_round + 1, turn=first, first=first,
            hands=hands, kitty=kitty, discard=[], trick=[], tricks=[], taker=None,
            passed=[], captured=[[], []], adjust2=[0, 0], draft=[], exposed_trumps=[],
            contract=None, bids=[None] * 4, called=[False] * 4, kitty_revealed=False,
            **retained, result=None, excuse_note='', notice=(reason + ' ' if reason else '') +
            (f'检测到 Petit sec，已重发 {rejected} 次。' if rejected else '') + '发牌完成，轮流叫牌。')

    def action(self, seat, action, payload=None):
        s = self.data
        payload = payload or {}
        if seat != s['turn']:
            raise RuleError('还没轮到你，请等待当前玩家')
        if s['phase'] == 'bidding' and action == 'bid':
            bid = payload.get('bid')
            if bid == 'take':  # Accept an older client's Prise label during refresh.
                bid = 'prise'
            if bid != 'pass' and bid not in CONTRACTS:
                raise RuleError('请选择不叫或有效合同')
            if bid != 'pass':
                current = CONTRACTS[s['contract']]['multiplier'] if s['contract'] else 0
                if CONTRACTS[bid]['multiplier'] <= current:
                    raise RuleError('加叫必须高于当前最高合同')
                s.update(taker=seat, contract=bid)
                s['bids'][seat] = bid
                s['called'][seat] = True
            else:
                s['passed'].append(seat)
            challengers = [i for i in range(4) if i not in s['passed'] and i != s['taker']]
            if bid == 'contre' or not challengers:
                self.finish_bidding()
            else:
                s['turn'] = next((seat + step) % 4 for step in range(1, 5)
                                 if (seat + step) % 4 in challengers)
                s['notice'] = '继续轮流加叫；不叫后退出本轮竞价。'
        elif s['phase'] == 'discard' and action in ('draft', 'discard'):
            selected = payload.get('cards')
            validate_discard(s['hands'][seat], selected, complete=action == 'discard')
            if action == 'draft':
                s['draft'] = list(selected)
                return
            s['discard'] = list(selected)
            s['exposed_trumps'] = [c for c in selected if c.startswith('T')]
            s['hands'][seat] = sort_hand([c for c in s['hands'][seat] if c not in selected] + s['kitty'])
            s.update(phase='playing', turn=s['first'], draft=[], notice='扣牌完成，底牌已收入。开始第一墩。')
        elif s['phase'] == 'playing' and action == 'play':
            card = payload.get('card')
            if card not in legal_plays(s['hands'][seat], s['trick']):
                raise RuleError('这张牌现在不能出：请跟花或在缺门时出将')
            s['hands'][seat].remove(card)
            s['trick'].append(dict(seat=seat, card=card))
            if len(s['trick']) == 4:
                s.update(phase='collecting', turn=None, notice='本墩已出齐，正在收墩。')
            else:
                s['turn'] = (seat + 1) % 4
                s['notice'] = '请按顺序出牌。可以出小将，无需压过桌面将牌。'
        else:
            raise RuleError('当前阶段不支持这个操作')

    def collect(self):
        s = self.data
        if s['phase'] != 'collecting':
            raise RuleError('本墩尚未出齐')
        plays = s['trick']
        winner = trick_winner(plays)
        fool = next((p for p in plays if p['card'] == 'F'), None)
        last = len(s['tricks']) == 17
        fool_slam = False
        if last and fool:
            fool_team = 0 if fool['seat'] == s['taker'] else 1
            previous_teams = [0 if t['winner'] == s['taker'] else 1 for t in s['tricks']]
            if all(team == fool_team for team in previous_teams):
                winner = fool['seat']
                fool_slam = True
        winner_team = 0 if winner == s['taker'] else 1
        for play in plays:
            if play['card'] != 'F':
                s['captured'][winner_team].append(play['card'])
                continue
            owner = 0 if play['seat'] == s['taker'] else 1
            if owner != winner_team:
                s['adjust2'][owner] -= 1
                s['adjust2'][winner_team] += 1
                s['excuse_note'] = '愚人保留给原阵营，并向吃墩方补偿 0.5 分。'
            else:
                s['excuse_note'] = '愚人以大满贯例外赢得末墩。' if fool_slam else '愚人与本墩均归原阵营，无需补偿。'
            s['captured'][owner].append('F')
        s['tricks'].append(dict(plays=deepcopy(plays), winner=winner, fool_slam=fool_slam,
                               kept_fool=fool['seat'] if fool and fool['seat'] != winner else None))
        s['trick'] = []
        if last:
            s.update(phase='finished', turn=None)
            s['result'] = settle(s)
            self.finish_awards()
            s['notice'] = '本局结束，查看计分明细。'
        else:
            s.update(phase='playing', turn=winner, notice='上一墩已收齐，由赢家先出。')

    def timeout(self):
        s = self.data
        if s['phase'] == 'bidding':
            self.action(s['turn'], 'bid', {'bid': 'pass'})
        elif s['phase'] == 'discard':
            self.action(s['turn'], 'discard', {'cards': auto_discard(s['hands'][s['turn']], s['draft'])})
        elif s['phase'] == 'playing':
            self.action(s['turn'], 'play', {'card': RNG.choice(legal_plays(s['hands'][s['turn']], s['trick']))})
        elif s['phase'] == 'collecting':
            self.collect()
