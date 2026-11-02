"""Public policy interface and built-in local bot registry."""
from .bot_policy import BotDecision, BotPolicy, RandomPolicy, observation_from_view
from .bot_strategy import NormalPolicy
from .bot_search import AdvancedPolicy


POLICY_LABELS = {'random': '基础', 'normal': '普通', 'advanced': '进阶'}
POLICIES: dict[str, BotPolicy] = {
    'random': RandomPolicy(),
    'normal': NormalPolicy(),
    'advanced': AdvancedPolicy(),
}
