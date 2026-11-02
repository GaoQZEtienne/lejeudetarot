"""Public policy interface and built-in local bot registry."""
from .bot_policy import BotDecision, BotPolicy, observation_from_view
from .bot_strategy import NormalPolicy
from .bot_search import AdvancedPolicy


POLICY_LABELS = {'normal': '普通', 'advanced': '进阶'}
POLICIES: dict[str, BotPolicy] = {
    'normal': NormalPolicy(),
    'advanced': AdvancedPolicy(),
}
