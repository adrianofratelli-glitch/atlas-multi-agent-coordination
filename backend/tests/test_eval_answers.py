"""eval_answers: matemática de pass@k / pass^k, notas de trajetória e o caminho --offline ponta a ponta."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

import eval_answers as ea


def test_pass_at_k_e_pass_hat_k_com_k_igual_a_n_sao_os_empiricos():
    assert ea.pass_at_k(3, 3, 3) == 1.0 and ea.pass_hat_k(3, 3, 3) == 1.0
    assert ea.pass_at_k(3, 1, 3) == 1.0 and ea.pass_hat_k(3, 1, 3) == 0.0
    assert ea.pass_at_k(3, 0, 3) == 0.0 and ea.pass_hat_k(3, 0, 3) == 0.0


def test_estimadores_sem_vies_para_k_menor_que_n():
    # 4 trials, 2 sucessos: pass@2 = 1 - C(2,2)/C(4,2) = 5/6; pass^2 = C(2,2)/C(4,2) = 1/6
    assert ea.pass_at_k(4, 2, 2) == pytest.approx(5 / 6)
    assert ea.pass_hat_k(4, 2, 2) == pytest.approx(1 / 6)
    assert ea.pass_at_k(4, 2, 1) == pytest.approx(0.5) == ea.pass_hat_k(4, 2, 1)
    with pytest.raises(ValueError):
        ea.pass_hat_k(2, 1, 3)


def _trial(passed, media=4.5, traj=True):
    return {"passed": passed, "checks": {"trajectory": traj, "judge": passed}, "judge": {"available": True, "media": media},
            "latency_ms": 1000.0, "tokens": 100, "cost_usd": 0.1}


def test_resumo_por_caso_e_total_separa_acerto_ocasional_de_consistencia():
    flaky = ea.summarize_case("flaky", [_trial(True), _trial(False, 3.0)])
    solid = ea.summarize_case("solid", [_trial(True), _trial(True)])
    assert flaky["pass_at_k"] == 1.0 and flaky["pass_hat_k"] == 0.0 and flaky["failures"] == ["judge"]
    assert solid["pass_hat_k"] == 1.0 and solid["judge_media"] == 4.5
    run = ea.summarize_run([flaky, solid])
    assert run["pass_at_k"] == 1.0 and run["pass_hat_k"] == 0.5 and run["trial_pass_rate"] == 0.75
    assert run["total_cost_usd"] == pytest.approx(0.4)


def _response(path, *, active="order_agent", awaiting=None, timeline=()):
    return SimpleNamespace(agent_path=path, timeline=list(timeline), active_agent=active, awaiting_approval=awaiting,
                           degraded=False, response="ok")


def test_trajetoria_subconjunto_ordem_estrita_e_proibidos():
    case = {"expected_agents": ["support_agent", "order_agent"], "strict_order": True, "forbidden_agents": ["billing_agent"]}
    ok = ea.grade_trajectory(case, _response(["support_agent", "product_agent", "order_agent"]),
                             ["support_agent", "product_agent", "order_agent"])
    assert all(ok.values())
    swapped = ea.grade_trajectory(case, _response([]), ["order_agent", "support_agent"])
    assert swapped["expected_agents"] and not swapped["strict_order"]
    forbidden = ea.grade_trajectory(case, _response([]), ["support_agent", "order_agent", "billing_agent"])
    assert not forbidden["no_forbidden_agent"]
    # pausa HITL inesperada reprova; esperada aprova
    paused = ea.grade_trajectory({"expected_agents": []}, _response([], awaiting={"tool": "x"}), [])
    assert not paused["awaiting_approval"]


async def test_offline_ponta_a_ponta_passa_e_detecta_escrita_ausente(tmp_path):
    report = await ea.run(offline=True, trials=2, log=lambda *_: None)
    summary = report["summary"]
    assert summary["mode"] == "offline" and summary["cases"] == 10 and summary["k"] == 2
    assert summary["pass_hat_k"] == 1.0, [row for row in report["cases"] if row["pass_hat_k"] < 1]
    refund = report["trials"]["ans-diego-refund-hitl"][0]
    assert refund["awaiting_approval"]["tool"] == "request_order_status_change"

    # Sem o roteiro de escrita, o caso de resgate tem de reprovar pelo EFEITO, não pela rota.
    dataset = json.loads(ea.DATASET.read_text(encoding="utf-8"))
    for case in dataset["cases"]:
        case.pop("offline_script", None)
    path = tmp_path / "ds.json"
    path.write_text(json.dumps(dataset), encoding="utf-8")
    broken = await ea.run(offline=True, trials=1, only=["ans-carla-loyalty-redemption"], dataset_path=path,
                          log=lambda *_: None)
    row = broken["cases"][0]
    assert row["pass_hat_k"] == 0.0 and row["trajectory_rate"] == 1.0
    assert any(name.startswith("state[") for name in row["failures"])
