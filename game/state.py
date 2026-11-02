from copy import deepcopy

from .cards import RNG, deal, deck, petit_sec, sort_hand
from .rules import RuleError, auto_discard, legal_plays, trick_winner, validate_discard
from .scoring import settle


class Game:
    def __init__(self, data=None):
        self.data = deepcopy(data) if data else dict(phase='lobby', round=0, turn=None,
            hands=[[], [], [], []], kitty=[], discard=[], trick=[], tricks=[], taker=None,
            first=None, passed=[], captured=[[], []], adjust2=[0, 0], draft=[],
            exposed_trumps=[], result=None, excuse_note='', notice='四位好友入座后，由房主开始。')

    def new_deal(self, reason=''):
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
            result=None, excuse_note='', notice=(reason + ' ' if reason else '') +
            (f'检测到 Petit sec，已重发 {rejected} 次。' if rejected else '') + '发牌完成，轮流叫牌。')

    def action(self, seat, action, payload=None):
        s = self.data
        payload = payload or {}
        if seat != s['turn']:
            raise RuleError('还没轮到你，请等待当前玩家')
        if s['phase'] == 'bidding' and action == 'bid':
            bid = payload.get('bid')
            if bid not in ('pass', 'take'):
                raise RuleError('请选择不叫或 Petit ×1')
            if bid == 'take':
                s.update(taker=seat, phase='discard', notice='底牌已公开；请从原手牌中扣六张。')
            else:
                s['passed'].append(seat)
                if len(s['passed']) == 4:
                    self.new_deal('四人均不叫，重新发牌。')
                else:
                    s['turn'] = (seat + 1) % 4
                    s['notice'] = '上一位玩家不叫，请继续叫牌。'
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
            if last and not fool_slam:
                owner = 1 - owner
                s['excuse_note'] = '愚人在最后一墩打出，已转交对方阵营。'
            elif not last and owner != winner_team:
                s['adjust2'][owner] -= 1
                s['adjust2'][winner_team] += 1
                s['excuse_note'] = '愚人保留给原阵营，并向吃墩方补偿 0.5 分。'
            else:
                s['excuse_note'] = '愚人以大满贯例外赢得末墩。' if fool_slam else '愚人与本墩均归原阵营，无需补偿。'
            s['captured'][owner].append('F')
        s['tricks'].append(dict(plays=deepcopy(plays), winner=winner, fool_slam=fool_slam))
        s['trick'] = []
        if last:
            s.update(phase='finished', turn=None)
            s['result'] = settle(s)
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
