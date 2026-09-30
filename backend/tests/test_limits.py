import pytest

from app.budget import BudgetExceeded, TurnBudget
from app.rate_limit import SlidingWindowLimiter


def test_budget_isolated_per_agent_and_global():
    budget = TurnBudget(100, {"a": 60, "b": 80})
    budget.reserve("a", 55)
    budget.reserve("b", 40)
    with pytest.raises(BudgetExceeded, match="global"):
        budget.reserve("b", 10)


def test_agent_budget_blocks_single_agent():
    budget = TurnBudget(200, {"a": 50})
    with pytest.raises(BudgetExceeded, match="agente"):
        budget.reserve("a", 51)


def test_sliding_window_prunes_old_entries():
    limiter = SlidingWindowLimiter(limit=2, window_seconds=10)
    assert limiter.allow("ana", now=0)
    assert limiter.allow("ana", now=1)
    assert not limiter.allow("ana", now=2)
    assert limiter.allow("ana", now=11)

