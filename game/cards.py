from random import SystemRandom

RNG = SystemRandom()
SUITS = ('S', 'H', 'D', 'C')
BOUTS = {'T1', 'T21', 'F'}


def deck():
    return [f'{s}{n}' for s in SUITS for n in range(1, 15)] + [f'T{n}' for n in range(1, 22)] + ['F']


def rank(card):
    return 0 if card == 'F' else int(card[1:])


def points2(card):
    """All arithmetic uses integer half-points."""
    if card in BOUTS:
        return 9
    return 2 * (rank(card) - 10) + 1 if card[0] in SUITS and rank(card) > 10 else 1


def sort_hand(cards):
    return sorted(cards, key=lambda c: ('SHDCTF'.index(c[0]), rank(c)))


def deal(cards=None):
    cards = list(cards) if cards is not None else deck()
    if len(cards) != 78 or set(cards) != set(deck()):
        raise ValueError('牌组必须包含全部 78 张不同的牌')
    hands, kitty = [[] for _ in range(4)], []
    offset = 0
    for _ in range(6):
        for seat in range(4):
            hands[seat].extend(cards[offset:offset + 3])
            offset += 3
        kitty.append(cards[offset])
        offset += 1
    return [sort_hand(h) for h in hands], kitty


def petit_sec(hand):
    return 'T1' in hand and 'F' not in hand and sum(c.startswith('T') for c in hand) == 1
