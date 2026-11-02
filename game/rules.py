from .cards import BOUTS, RNG, SUITS, rank


class RuleError(ValueError):
    pass


def lead_suit(trick):
    return next((p['card'][0] for p in trick if p['card'] != 'F'), None)


def legal_plays(hand, trick):
    suit = lead_suit(trick)
    if suit is None:
        return list(hand)
    following = [c for c in hand if c[0] == suit]
    trumps = [c for c in hand if c[0] == 'T']
    # House rule: following/cutting is mandatory, overtrumping is not.
    required = following or trumps or [c for c in hand if c != 'F']
    return required + (['F'] if 'F' in hand else [])


def discard_options(hand):
    plain = [c for c in hand if c[0] in SUITS and rank(c) != 14]
    trumps = [c for c in hand if c[0] == 'T' and c not in BOUTS]
    return plain, trumps if len(plain) < 6 else []


def validate_discard(hand, selected, complete=True):
    if not isinstance(selected, list) or any(not isinstance(c, str) for c in selected):
        raise RuleError('请选择手牌')
    if len(selected) != len(set(selected)) or len(selected) > 6 or (complete and len(selected) != 6):
        raise RuleError('必须选择六张不同的牌')
    plain, trumps = discard_options(hand)
    if not set(selected) <= set(plain + trumps):
        raise RuleError('只能扣原手牌；不能扣国王、Bout 或非必要的将牌')
    trump_count = sum(c.startswith('T') for c in selected)
    required_trumps = max(0, 6 - len(plain))
    if trump_count > required_trumps or (complete and trump_count != required_trumps):
        raise RuleError('须先扣普通花色牌，只能用将牌补足六张')
    return selected


def auto_discard(hand, draft=()):
    try:
        selected = list(validate_discard(hand, list(draft), complete=False))
    except RuleError:
        selected = []
    plain, trumps = discard_options(hand)
    pool = [c for c in plain if c not in selected]
    RNG.shuffle(pool)
    selected.extend(pool[:6 - len(selected)])
    pool = [c for c in trumps if c not in selected]
    RNG.shuffle(pool)
    selected.extend(pool[:6 - len(selected)])
    return validate_discard(hand, selected)


def trick_winner(trick):
    suit = lead_suit(trick)
    candidates = [p for p in trick if p['card'][0] == 'T']
    if not candidates:
        candidates = [p for p in trick if p['card'][0] == suit]
    return max(candidates, key=lambda p: rank(p['card']))['seat']
