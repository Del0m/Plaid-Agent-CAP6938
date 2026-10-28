import json
from datetime import date
from types import SimpleNamespace

from eval.budget_accuracy import run, run_profile, score
from eval.budget_users import PROFILES


def profile(pid):
    return next(p for p in PROFILES if p.id == pid)


def test_score_counts():
    lines = [SimpleNamespace(category="FOOD_AND_DRINK", kind="flexible", limit_cents=110),
             SimpleNamespace(category="SHOPPING", kind="flexible", limit_cents=50)]
    s = score(lines, {"FOOD_AND_DRINK": 100, "TRAVEL": 30})
    assert s["mape"] == 10.0
    assert s["zero_actual_lines"] == ["SHOPPING"]
    assert s["unbudgeted"] == ["TRAVEL"]
    assert s["lines"][0] == {"category": "FOOD_AND_DRINK", "kind": "flexible", "limit_cents": 110,
                             "actual_cents": 100}


def test_run_writes_results(tmp_path, capsys):
    path = run([profile("steady"), profile("vacation")], tmp_path, date(2026, 10, 28))
    assert path == tmp_path / "2026-10-28" / "budget_accuracy" / "results.json"
    data = json.loads(path.read_text())
    vac = next(p for p in data["profiles"] if p["id"] == "vacation")
    assert vac["median"]["mape"] < vac["mean"]["mape"]
    assert set(data["pooled"]) == {"median", "mean"}
    assert "vacation" in capsys.readouterr().out


def test_stored_budget_matches_median_fit():
    r = run_profile(profile("vacation"))
    stored = {ln["category"]: ln["limit_cents"] for ln in r["budget"]["lines"]}
    assert stored == {ln["category"]: ln["limit_cents"] for ln in r["median"]["lines"]}


def test_early_rent_fixed_line_is_one_rent():
    rent = next(ln for ln in run_profile(profile("early-rent"))["median"]["lines"]
                if ln["category"] == "RENT_AND_UTILITIES")
    assert rent["limit_cents"] == 150000 and rent["actual_cents"] == 150000


def test_occasional_mean_has_zero_actual_line():
    r = run_profile(profile("occasional"))
    assert "GENERAL_MERCHANDISE" in r["mean"]["zero_actual_lines"]
    assert "GENERAL_MERCHANDISE" not in [ln["category"] for ln in r["median"]["lines"]]
    assert r["budget"]["skipped"] == ["GENERAL_MERCHANDISE"]
