# ported from deal-terms-desk tests/test_budget.py on 2026-11-05; the ledger takes dollars instead of prompt chars and usage
import sqlite3
import threading
from datetime import datetime, timezone

import pytest

from askmike.service.budget import STALE_S, Budget

WORST = 0.03  # one call's worst case in dollars


class Clock:
    def __init__(self, *ymdhm):
        self.t = datetime(*ymdhm, tzinfo=timezone.utc).timestamp()

    def __call__(self):
        return self.t


def budget(tmp_path, month=1.0, day=1.0, clock=None):
    return Budget(tmp_path / "spend.db", month, day, clock or Clock(2026, 10, 5, 12, 0))


def statuses(tmp_path):
    return [r[0] for r in sqlite3.connect(tmp_path / "spend.db").execute("SELECT status FROM spend ORDER BY id")]


def _rows(tmp_path):
    return sqlite3.connect(tmp_path / "spend.db").execute("SELECT id, status, usd FROM spend ORDER BY id").fetchall()


def test_a_reservation_counts_at_worst_until_settled_at_actual(tmp_path):
    b = budget(tmp_path)
    rid = b.reserve(WORST)
    assert b.spent()["month"] == pytest.approx(0.03)
    assert b.settle(rid, 0.0015) == pytest.approx(0.0015)
    assert b.spent() == {"month": pytest.approx(0.0015), "day": pytest.approx(0.0015)}
    assert statuses(tmp_path) == ["settled"]


def test_unknown_usage_settles_at_the_worst_case(tmp_path):
    b = budget(tmp_path)
    rid = b.reserve(WORST)
    assert b.settle(rid, None) == pytest.approx(0.03)  # a timeout: the call may have been billed in full


def test_two_models_reserve_different_worst_cases(tmp_path):
    b = budget(tmp_path)
    b.reserve(0.01)
    b.reserve(0.10)
    assert b.spent()["month"] == pytest.approx(0.11)


def test_release_frees_the_headroom(tmp_path):
    b = budget(tmp_path, month=0.05)
    first = b.reserve(WORST)
    assert first is not None and b.reserve(WORST) is None
    b.release(first)
    assert b.reserve(WORST) is not None
    assert statuses(tmp_path) == ["released", "open"]


def test_the_day_cap_resets_at_utc_midnight(tmp_path):
    clock = Clock(2026, 10, 5, 23, 59)
    b = budget(tmp_path, month=1.0, day=0.05, clock=clock)
    assert b.reserve(WORST) is not None and b.reserve(WORST) is None
    clock.t += 120  # 00:01 UTC the next day
    assert b.reserve(WORST) is not None
    assert b.spent()["day"] == pytest.approx(0.03) and b.spent()["month"] == pytest.approx(0.06)


def test_the_month_cap_resets_on_the_first_in_utc(tmp_path):
    clock = Clock(2026, 10, 31, 23, 30)
    b = budget(tmp_path, month=0.05, day=1.0, clock=clock)
    b.settle(b.reserve(WORST), 0.04)
    assert b.reserve(WORST) is None
    clock.t += 3600  # 00:30 UTC on 1 November
    assert b.spent()["month"] == 0.0 and b.reserve(WORST) is not None


def _race(budgets, n=50):
    got, errors, start = [], [], threading.Barrier(n)

    def go(b):
        try:
            start.wait()
            got.append(b.reserve(WORST))
        except BaseException as e:  # a thread that raised must fail the test, not just go missing
            errors.append(e)
    threads = [threading.Thread(target=go, args=(budgets[k % len(budgets)],)) for k in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == [] and len(got) == n
    return got


def test_fifty_threads_never_overshoot_the_cap(tmp_path):
    b = budget(tmp_path)
    got = _race([b])
    assert sum(r is not None for r in got) == 33  # 33 x $0.03 fits under $1; a 34th would not
    assert b.spent()["month"] <= 1.0


def test_two_processes_sharing_the_ledger_never_overshoot(tmp_path):
    a, b = budget(tmp_path), budget(tmp_path)  # two connections, as two processes on one file
    got = _race([a, b])
    assert sum(r is not None for r in got) == 33 and a.spent()["month"] <= 1.0


def test_spend_survives_a_restart(tmp_path):
    clock = Clock(2026, 10, 5, 12, 0)
    b = budget(tmp_path, clock=clock)
    b.settle(b.reserve(WORST), 0.0015)
    assert budget(tmp_path, clock=clock).spent()["month"] == pytest.approx(0.0015)


def test_a_crashed_reservation_is_settled_at_worst_once_stale(tmp_path):
    clock = Clock(2026, 10, 5, 12, 0)
    budget(tmp_path, clock=clock).reserve(WORST)  # the process dies before settle
    budget(tmp_path, clock=clock)  # a process starting now: that call may still be in flight elsewhere
    assert statuses(tmp_path) == ["open"]
    clock.t += STALE_S + 1
    b = budget(tmp_path, clock=clock)
    assert statuses(tmp_path) == ["settled"] and b.spent()["month"] == pytest.approx(0.03)


def test_state_is_reached_when_less_than_one_call_is_left(tmp_path):
    b = budget(tmp_path, month=0.05, day=1.0)
    assert b.state(min_call_usd=0.03) == "ok"
    b.reserve(WORST)
    assert b.state(min_call_usd=0.03) == "reached"  # $0.02 left is less than one call


def test_state_is_reached_on_the_day_cap_too(tmp_path):
    b = budget(tmp_path, month=10.0, day=0.05)
    b.reserve(WORST)
    assert b.state(min_call_usd=0.03) == "reached"


def test_settle_with_no_reservation_id_raises(tmp_path):
    b = budget(tmp_path)
    with pytest.raises(ValueError, match="None"):
        b.settle(None, 0.01)
    assert _rows(tmp_path) == []


def test_settle_on_an_unknown_id_raises_and_changes_nothing(tmp_path):
    b = budget(tmp_path)
    rid = b.reserve(WORST)
    with pytest.raises(ValueError, match="999"):
        b.settle(999, 0.01)
    with pytest.raises(ValueError, match="999"):
        b.settle(999, None)
    assert _rows(tmp_path) == [(rid, "open", 0.0)]
    assert b.spent()["month"] == pytest.approx(0.03)


def test_settle_on_a_released_row_raises_and_it_stays_released(tmp_path):
    b = budget(tmp_path)
    rid = b.reserve(WORST)
    b.release(rid)
    for usd in (0.01, None):
        with pytest.raises(ValueError, match=str(rid)):
            b.settle(rid, usd)
    assert _rows(tmp_path) == [(rid, "released", 0.0)]


def test_a_stale_reconciled_row_can_still_be_settled_at_actual(tmp_path):
    clock = Clock(2026, 10, 5, 12, 0)
    b = budget(tmp_path, clock=clock)
    rid = b.reserve(WORST)
    clock.t += STALE_S + 1
    b.reserve(WORST)  # reconciles the first row at worst
    assert _rows(tmp_path)[0][1:] == ("settled", pytest.approx(0.03))
    assert b.settle(rid, 0.0015) == pytest.approx(0.0015)
    assert _rows(tmp_path)[0][1:] == ("settled", pytest.approx(0.0015))


def test_reserve_refuses_a_worst_case_that_is_not_a_positive_finite_number(tmp_path):
    """A zero, negative or non-finite worst case would open headroom or poison the totals: refused, nothing booked."""
    b = budget(tmp_path)
    for bad in (0, 0.0, -0.01, float("nan"), float("inf"), float("-inf")):
        with pytest.raises(ValueError, match="worst"):
            b.reserve(bad)
    assert _rows(tmp_path) == [] and b.spent()["month"] == 0


def test_settle_refuses_a_negative_or_non_finite_amount_and_leaves_the_row_open(tmp_path):
    b = budget(tmp_path)
    rid = b.reserve(WORST)
    for bad in (-0.01, float("nan"), float("inf"), float("-inf")):
        with pytest.raises(ValueError, match="usd"):
            b.settle(rid, bad)
    assert _rows(tmp_path) == [(rid, "open", 0.0)] and b.spent()["month"] == pytest.approx(WORST)
    assert b.settle(rid, 0.0) == 0.0  # a free call is a real figure; None (unknown) stays allowed
    assert b.settle(b.reserve(WORST), None) == pytest.approx(WORST)


def test_settling_above_the_worst_case_books_the_actual_bill(tmp_path):
    """The worst case is an estimate; a call that cost more was billed that much, and the ledger says so."""
    b = budget(tmp_path, month=1.0, day=1.0)
    rid = b.reserve(WORST)
    assert b.settle(rid, 0.05) == pytest.approx(0.05)
    assert _rows(tmp_path) == [(rid, "settled", pytest.approx(0.05))]
    assert b.spent() == {"month": pytest.approx(0.05), "day": pytest.approx(0.05)}
