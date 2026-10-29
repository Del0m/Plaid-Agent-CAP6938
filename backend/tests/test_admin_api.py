from datetime import date
from types import SimpleNamespace

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from app import config
from app.api import budget as budget_routes
from app.api import budget_hooks
from app.budget_service.daily import user_lock
from app.db.models import AgentRun, User
from eval.budget_users import PLANTED, seed_profile
from tests.fakes import SchemaReplyModel, ScriptedChatModel


@pytest.fixture
def admin(client, db, monkeypatch, tmp_path):
    ck = InMemorySaver()
    monkeypatch.setattr(budget_hooks, "session_factory", db)
    monkeypatch.setattr(budget_hooks, "get_checkpointer", lambda: ck)
    monkeypatch.setattr(budget_hooks, "make_model", SchemaReplyModel)
    monkeypatch.setattr(budget_routes, "_spawn", lambda work: work())      # one test db connection; run inline
    monkeypatch.setattr(config, "PROMPT_AUDIT_PATH", tmp_path / "audit.jsonl")
    monkeypatch.setattr(config, "ENABLE_ADMIN", True)

    def seed(pid="overspend-one-time", until=date(2026, 9, 30)):
        with db() as s:
            uid = seed_profile(s, next(p for p in PLANTED if p.id == pid), until=until)
        monkeypatch.setattr(config, "DEV_USER_EMAIL", f"{pid}@eval.local")
        return uid

    return SimpleNamespace(client=client, db=db, seed=seed, mp=monkeypatch)


def advance(admin, days=3):
    r = admin.client.post("/admin/sandbox/advance", json={"days": days})
    return r


def test_admin_off_404(admin):
    admin.seed()
    admin.mp.setattr(config, "ENABLE_ADMIN", False)
    for r in (admin.client.get("/admin/sandbox"), advance(admin), admin.client.get("/admin/sandbox/advance/1")):
        assert (r.status_code, r.json()["detail"]) == (404, "Not found.")


def test_sandbox_state(admin):
    admin.seed()
    assert admin.client.get("/admin/sandbox").json() == {
        "simulated": True, "profile": "overspend-one-time", "date": "2026-09-30"}


def test_sandbox_state_real_user(admin):
    with admin.db() as s:
        seed_profile(s, next(p for p in PLANTED if p.id == "overspend-recurring"))
    admin.mp.setattr(config, "DEV_USER_EMAIL", "overspend-recurring@eval.local")
    assert admin.client.get("/admin/sandbox").json() == {"simulated": False, "profile": None, "date": None}


def test_advance_202_then_ok(admin):
    admin.seed()
    r = advance(admin, 3)
    assert r.status_code == 202
    job = admin.client.get(f"/admin/sandbox/advance/{r.json()['job_id']}").json()
    assert (job["status"], job["days_requested"], job["days_done"], job["start_date"], job["date"]) == (
        "ok", 3, 3, "2026-09-30", "2026-10-03")
    assert len(job["run_ids"]) == 3 and job["error"] is None
    assert admin.client.get("/admin/sandbox").json()["date"] == "2026-10-03"


def test_advance_not_simulated_409(admin):
    with admin.db() as s:
        seed_profile(s, next(p for p in PLANTED if p.id == "overspend-recurring"))
    admin.mp.setattr(config, "DEV_USER_EMAIL", "overspend-recurring@eval.local")
    r = advance(admin)
    assert (r.status_code, r.json()["detail"]) == (409, "Only simulated users can be advanced.")


def test_advance_locked_409(admin):
    uid = admin.seed()
    lock = user_lock(uid)
    lock.acquire()
    try:
        r = advance(admin)
    finally:
        lock.release()
    assert (r.status_code, r.json()["detail"]) == (409, "A budget run is already in progress.")


def test_advance_days_422(admin):
    admin.seed()
    assert advance(admin, 0).status_code == 422 and advance(admin, 32).status_code == 422


def test_other_users_job_404(admin):
    admin.seed()
    job_id = advance(admin, 1).json()["job_id"]
    admin.seed("overspend-recurring")
    r = admin.client.get(f"/admin/sandbox/advance/{job_id}")
    assert (r.status_code, r.json()["detail"]) == (404, "Not found.")


def test_start_failure_500_frees_lock(admin):
    uid = admin.seed()

    def broken(*a, **k):
        raise RuntimeError("database is locked")

    admin.mp.setattr(budget_routes, "_spawn", broken)
    r = advance(admin)
    assert (r.status_code, r.json()["detail"]) == (500, "The simulation couldn't start.")
    lock = user_lock(uid)
    assert lock.acquire(blocking=False)
    lock.release()


def test_advance_job_error_reports_type(admin):
    uid = admin.seed()
    admin.mp.setattr(budget_hooks, "make_model",
                     lambda: ScriptedChatModel(replies=[ConnectionError("down")] * 5))
    # the first october days have no flags (no model call), so start near the purchase
    with admin.db() as s:
        user = s.get(User, uid)
        user.prefs_json = {"sim": {"profile": "overspend-one-time", "date": "2026-10-11"}}
        s.commit()
    job_id = advance(admin, 3).json()["job_id"]
    job = admin.client.get(f"/admin/sandbox/advance/{job_id}").json()
    assert (job["status"], job["error"]) == ("error", "ConnectionError")
    assert admin.client.get("/admin/sandbox").json()["date"] == job["date"]
    lock = user_lock(uid)
    assert lock.acquire(blocking=False)
    lock.release()


def test_lock_freed_when_final_status_write_fails(admin, monkeypatch):
    # review finding: the job thread released the lock only after writing its final status; a failing write
    # (sqlite "database is locked") left the user locked out until a restart
    from app.api import admin as admin_routes
    uid = admin.seed()
    real_utcnow = admin_routes.utcnow

    def broken_utcnow():
        raise RuntimeError("database is locked")

    monkeypatch.setattr(admin_routes, "utcnow", broken_utcnow)

    # a real thread, as in production (joined at once, so the single test db connection is never shared):
    # run inline, the exception would climb back into the route, whose fallback hides the leak
    def threaded(work):
        import threading
        t = threading.Thread(target=work)
        t.start()
        t.join()

    monkeypatch.setattr(budget_routes, "_spawn", threaded)
    advance(admin, 1)
    monkeypatch.setattr(admin_routes, "utcnow", real_utcnow)
    lock = user_lock(uid)
    assert lock.acquire(blocking=False)
    lock.release()
