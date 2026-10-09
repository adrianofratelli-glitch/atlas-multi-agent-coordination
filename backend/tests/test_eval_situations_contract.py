"""`eval_situations.py` não pode terminar 0 com caso errado (juiz 2026-10-08: 282/287 e exit 0)."""

import eval_situations as ev


def row(ok=True, error=None):
    return {"ok": ok, "error": error}


def test_all_correct_exits_zero():
    assert ev.exit_code([row(), row()]) == 0


def test_any_wrong_case_exits_nonzero():
    assert ev.exit_code([row(), row(ok=False)]) == 1


def test_a_broken_turn_exits_two():
    assert ev.exit_code([row(), row(ok=False, error="TimeoutError")]) == 2
