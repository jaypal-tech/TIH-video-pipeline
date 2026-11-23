import math

import pytest

from tih.budget import BudgetExceeded, BudgetGuard
from tih.ledger import CallRecord, Ledger


def record(call_id, **fields):
    values = {
        "call_id": call_id,
        "timestamp": "2026-11-24T14:03:11Z",
        "step": "video",
        "provider": "fal",
        "model": "kling-v3",
        "status": "ok",
    }
    values.update(fields)
    return CallRecord(**values)


@pytest.fixture
def ledger_with_spend(tmp_path):
    def build(amount):
        ledger = Ledger(tmp_path / "calls.jsonl")
        if amount:
            ledger.append(record("spent", cost_usd=amount))
        return ledger

    return build


def test_budget_blocks_before_stop(ledger_with_spend):
    guard = BudgetGuard(ledger_with_spend(2999.0), warn=1500, stop=3000)
    with pytest.raises(BudgetExceeded):
        guard.reserve(1.01)


def test_budget_allows_exactly_to_stop(ledger_with_spend):
    guard = BudgetGuard(ledger_with_spend(2999.0), warn=1500, stop=3000)
    assert guard.reserve(1.0).amount == 1.0


def test_open_reservations_count(ledger_with_spend):
    guard = BudgetGuard(ledger_with_spend(0), warn=1500, stop=10)
    guard.reserve(6)
    with pytest.raises(BudgetExceeded):
        guard.reserve(6)


def test_released_reservation_frees_budget(ledger_with_spend):
    guard = BudgetGuard(ledger_with_spend(0), warn=1500, stop=10)
    reservation = guard.reserve(6)
    reservation.release()
    reservation.release()
    assert guard.reserve(10).amount == 10


def test_unfinished_call_counts_at_estimate(tmp_path):
    ledger = Ledger(tmp_path / "calls.jsonl")
    ledger.append(record("crashed", status="submitted", estimate_usd=9.0))
    guard = BudgetGuard(ledger, warn=1500, stop=10)
    with pytest.raises(BudgetExceeded):
        guard.reserve(1.01)


def test_actual_cost_replaces_estimate(tmp_path):
    ledger = Ledger(tmp_path / "calls.jsonl")
    ledger.append(record("c1", status="submitted", estimate_usd=9.0))
    ledger.update("c1", status="ok", cost_usd=1.0)
    guard = BudgetGuard(ledger, warn=1500, stop=10)
    assert guard.spent() == 1.0


def test_confirmed_free_call_costs_nothing(tmp_path):
    ledger = Ledger(tmp_path / "calls.jsonl")
    ledger.append(record("c1", status="blocked", estimate_usd=9.0, cost_usd=0.0))
    assert BudgetGuard(ledger).spent() == 0.0


def test_blocked_call_with_unknown_charge_counts_at_estimate(tmp_path):
    ledger = Ledger(tmp_path / "calls.jsonl")
    ledger.append(record("c1", status="blocked", estimate_usd=2.5))
    assert BudgetGuard(ledger).spent() == 2.5


def test_warning_flag_when_crossing_warn(ledger_with_spend):
    guard = BudgetGuard(ledger_with_spend(1499.0), warn=1500, stop=3000)
    assert guard.reserve(0.5).over_warn is False
    assert guard.reserve(1.0).over_warn is True


def test_scope_cap_blocks_only_that_scope(tmp_path):
    ledger = Ledger(tmp_path / "calls.jsonl")
    ledger.append(record("proving", step="proving", cost_usd=99.5))
    ledger.append(record("other", step="video", cost_usd=500.0))
    guard = BudgetGuard(
        ledger, scope_cap=100.0, scope_filter=lambda r: r.step == "proving"
    )
    assert guard.reserve(0.5).amount == 0.5
    with pytest.raises(BudgetExceeded, match="scope"):
        guard.reserve(0.01)


def test_scope_cap_requires_filter(tmp_path):
    with pytest.raises(ValueError, match="scope_filter"):
        BudgetGuard(Ledger(tmp_path / "calls.jsonl"), scope_cap=100.0)


@pytest.mark.parametrize("bad", [-1.0, math.nan, math.inf])
def test_invalid_estimate_is_rejected(ledger_with_spend, bad):
    guard = BudgetGuard(ledger_with_spend(0))
    with pytest.raises(ValueError):
        guard.reserve(bad)


def test_project_defaults(ledger_with_spend):
    guard = BudgetGuard(ledger_with_spend(0))
    assert (guard.warn, guard.stop) == (1500.0, 3000.0)
