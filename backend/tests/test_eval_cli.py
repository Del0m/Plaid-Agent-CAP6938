import json
from datetime import date

import pytest

from eval import run_eval
from eval.runner import RunRecord
from tests.fakes import FakeKB
from tests.test_eval_fixtures import write

DAY = date(2026, 10, 27)


def test_results_dir_sanitizes_and_never_overwrites(tmp_path):
    first = run_eval.results_dir(tmp_path, DAY, "qwen2.5:14b")
    second = run_eval.results_dir(tmp_path, DAY, "qwen2.5:14b")
    third = run_eval.results_dir(tmp_path, DAY, "qwen2.5:14b")

    assert first == tmp_path / "2026-10-27" / "qwen2.5_14b" and first.is_dir()
    assert (second.name, third.name) == ("qwen2.5_14b-2", "qwen2.5_14b-3")
    assert run_eval.results_dir(tmp_path, DAY, "a/b").name == "a_b"


def test_preflight_reports_each_problem(monkeypatch):
    monkeypatch.setattr(run_eval, "missing_models", lambda models: None)
    [down] = run_eval.preflight("qwen2.5:14b", "gemma3:12b", FakeKB())
    assert "isn't reachable" in down and "ollama serve" in down

    monkeypatch.setattr(run_eval, "missing_models", lambda models: ["gemma3:12b", "nomic-embed-text"])
    problems = run_eval.preflight("qwen2.5:14b", "gemma3:12b", FakeKB(chunks=0))
    assert "ollama pull gemma3:12b" in problems[0] and "ollama pull nomic-embed-text" in problems[1]
    assert "ingest_kb.py" in problems[2] and len(problems) == 3

    monkeypatch.setattr(run_eval, "missing_models", lambda models: [])
    assert run_eval.preflight("qwen2.5:14b", "gemma3:12b", FakeKB()) == []


# no real models, kb or scenarios: every outside dependency of main() is swapped out
@pytest.fixture
def offline(monkeypatch, tmp_path):
    scenarios = tmp_path / "scenarios"
    scenarios.mkdir()
    write(scenarios)
    write(scenarios, lambda d: d.update(id="second"))
    monkeypatch.setattr(run_eval, "SCENARIOS_DIR", scenarios)
    monkeypatch.setattr(run_eval, "RESULTS_DIR", tmp_path / "results")
    monkeypatch.setattr(run_eval, "KnowledgeBase", FakeKB)
    monkeypatch.setattr(run_eval, "get_chat_model", lambda **kw: object())
    monkeypatch.setattr(run_eval, "get_embeddings", lambda: object())
    monkeypatch.setattr(run_eval, "preflight", lambda *a: [])
    monkeypatch.setattr(run_eval, "score_run", lambda rec, sc, **kw: {
        "facts": {"found": 1, "total": 1, "passed": True, "score": 1.0, "missing": [], "forbidden_hits": []},
        "tools_ok": True, "sources_ok": True, "similarity": 0.9, "judge": None, "judge_passed": False})
    return tmp_path


def fake_run(scenario, *, run, **kw):
    return RunRecord(scenario_id=scenario.id, run=run, answer="ok", latency_s=1.0)


def test_main_returns_2_on_preflight_failure_and_runs_nothing(offline, monkeypatch, capsys):
    monkeypatch.setattr(run_eval, "preflight", lambda *a: ["Model x isn't pulled. Run: ollama pull x"])
    monkeypatch.setattr(run_eval, "run_once", lambda *a, **k: pytest.fail("ran a scenario"))

    assert run_eval.main([]) == 2
    assert "ollama pull x" in capsys.readouterr().out


def test_main_returns_2_on_bad_scenario(offline, monkeypatch, capsys):
    (run_eval.SCENARIOS_DIR / "broken.json").write_text("{not json")
    monkeypatch.setattr(run_eval, "run_once", lambda *a, **k: pytest.fail("ran a scenario"))

    assert run_eval.main([]) == 2
    assert "broken.json" in capsys.readouterr().out


def test_main_writes_runs_and_summary(offline, monkeypatch):
    monkeypatch.setattr(run_eval, "run_once", fake_run)

    assert run_eval.main(["--runs", "2", "--model", "m:1"]) == 0
    [out] = list((offline / "results").glob("*/m_1"))
    assert len(list(out.glob("*.run*.json"))) == 4
    summary = json.loads((out / "summary.json").read_text())
    cfg = json.loads((out / "config.json").read_text())
    assert summary["overall"]["runs"] == 4
    assert cfg["runs"] == 2 and cfg["model"] == "m:1" and cfg["judge"] == "gemma3:12b"
    assert cfg["scenarios"] == ["debt-highest-apr", "second"] and "finished_at" in cfg
    run1 = json.loads((out / "second.run1.json").read_text())
    assert run1["record"]["answer"] == "ok" and run1["scores"]["similarity"] == 0.9


def test_run_written_before_next_starts(offline, monkeypatch):
    calls = []

    def flaky(scenario, *, run, **kw):
        calls.append(run)
        if len(calls) == 2:
            raise KeyboardInterrupt
        return fake_run(scenario, run=run)

    monkeypatch.setattr(run_eval, "run_once", flaky)
    with pytest.raises(KeyboardInterrupt):
        run_eval.main(["--runs", "2"])
    [out] = list((offline / "results").glob("*/*"))
    assert (out / "debt-highest-apr.run1.json").exists()
