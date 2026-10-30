import json
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Optional

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.agents.tools import PFC_CATEGORIES
from app.db.base import Base
from app.db.models import Liability
from tests.factories import add_account, add_txn, add_user

# sqlite doesn't enforce the enum, so a typo like "credit card" would be stored as-is
LIABILITY_KINDS = tuple(Liability.__table__.c.kind.type.enums)

# scenario files -> validated dataclasses -> rows in a throwaway db (spec: tpc-16 scenario format).
# every problem names the file and field, so a typo fails loudly instead of scoring a silent 0


class ScenarioError(ValueError):
    pass


@dataclass(frozen=True)
class Fact:
    text: Optional[str] = None
    number: Optional[float] = None
    tol: float = 0.0

    @property
    def label(self) -> str:
        return self.text if self.text is not None else f"{self.number:g}±{self.tol:g}"


@dataclass(frozen=True)
class Expect:
    facts: list[Fact]
    forbid: list[str]
    tools: list[str]
    sources: list[str]


@dataclass(frozen=True)
class Scenario:
    id: str
    question: str
    user: dict
    golden_answer: str
    expect: Expect
    path: Path


def _fail(path: Path, field: str, problem: str):
    raise ScenarioError(f"{path.name}: {field}: {problem}")


def _require(path: Path, d: dict, key: str, kind, where: str = ""):
    field = f"{where}{key}"
    if key not in d:
        _fail(path, field, "missing")
    if not isinstance(d[key], kind) or (isinstance(d[key], str) and not d[key].strip()):
        _fail(path, field, "wrong type or empty")
    return d[key]


def _fact(path: Path, i: int, raw) -> Fact:
    field = f"expect.facts[{i}]"
    if not isinstance(raw, dict) or ("text" in raw) == ("number" in raw):
        _fail(path, field, "needs exactly one of text or number")
    if "text" in raw:
        # an empty text fact is a substring of every answer, so it would always pass
        if not isinstance(raw["text"], str) or not raw["text"].strip():
            _fail(path, field, "text must be a non-empty string")
        return Fact(text=raw["text"])
    try:
        return Fact(number=float(raw["number"]), tol=float(raw.get("tol", 0)))
    except (TypeError, ValueError):
        _fail(path, field, "number and tol must be numbers")


# a bare string would become a list of characters, and "" matches every answer
def _strings(path: Path, exp: dict, key: str) -> list[str]:
    value = exp.get(key, [])
    if not isinstance(value, list) or not all(isinstance(v, str) and v.strip() for v in value):
        _fail(path, f"expect.{key}", "must be a list of non-empty strings")
    return value


# every field seed() reads, so a typo fails the load (exit 2) instead of every run scoring a silent 0
def _check_liability(path: Path, raw: dict, where: str) -> None:
    if _require(path, raw, "kind", str, where) not in LIABILITY_KINDS:
        _fail(path, where + "kind", f"{raw['kind']!r} is not one of {', '.join(LIABILITY_KINDS)}")
    number = lambda v: isinstance(v, (int, float)) and not isinstance(v, bool)
    if raw.get("apr") is not None and not number(raw["apr"]):
        _fail(path, where + "apr", "must be a number or null")
    if "min_payment" in raw and not number(raw["min_payment"]):
        _fail(path, where + "min_payment", "must be a number")
    if "due_in_days" in raw and (not isinstance(raw["due_in_days"], int) or isinstance(raw["due_in_days"], bool)
                                 or raw["due_in_days"] < 0):
        _fail(path, where + "due_in_days", "must be a whole number, 0 or more")


def _check_user(path: Path, user: dict) -> None:
    accounts = _require(path, user, "accounts", list, "user.")
    names = set()
    for i, a in enumerate(accounts):
        where = f"user.accounts[{i}]."
        name = _require(path, a, "name", str, where)
        # the name is the account's plaid_account_id in seed(), so two with one name can't both be stored
        if name in names:
            _fail(path, where + "name", f"{name!r} is used by another account")
        names.add(name)
        _require(path, a, "type", str, where)
        _require(path, a, "balance", (int, float), where)
        if "liability" in a:
            _check_liability(path, _require(path, a, "liability", dict, where), where + "liability.")
    for i, t in enumerate(user.get("transactions", [])):
        where = f"user.transactions[{i}]."
        if _require(path, t, "account", str, where) not in names:
            _fail(path, where + "account", f"no account named {t['account']!r}")
        if _require(path, t, "category", str, where) not in PFC_CATEGORIES:
            _fail(path, where + "category", f"{t['category']!r} is not a Plaid primary category")
        if _require(path, t, "days_ago", int, where) < 0:
            _fail(path, where + "days_ago", "must be 0 or more")
        _require(path, t, "amount", (int, float), where)


def load_scenario(path: Path) -> Scenario:
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        _fail(path, "json", str(exc))
    if not isinstance(d, dict):
        _fail(path, "json", "top level must be an object")
    sid = _require(path, d, "id", str)
    if sid != path.stem:
        _fail(path, "id", f"{sid!r} must match the file name {path.stem!r}")
    question = _require(path, d, "question", str)
    golden = _require(path, d, "golden_answer", str)
    user = _require(path, d, "user", dict)
    _check_user(path, user)
    exp = _require(path, d, "expect", dict)
    facts = _require(path, exp, "facts", list, "expect.")
    if not facts:
        _fail(path, "expect.facts", "must not be empty")
    expect = Expect(facts=[_fact(path, i, f) for i, f in enumerate(facts)], forbid=_strings(path, exp, "forbid"),
                    tools=_strings(path, exp, "tools"), sources=_strings(path, exp, "sources"))
    return Scenario(id=sid, question=question, user=user, golden_answer=golden, expect=expect, path=path)


# ids equal file stems, so one folder can't hold two scenarios with the same id
def load_scenarios(directory: Path, only: Optional[str] = None) -> list[Scenario]:
    scenarios = [load_scenario(p) for p in sorted(directory.glob("*.json"))]
    if only is not None:
        scenarios = [s for s in scenarios if s.id == only]
        if not scenarios:
            raise ScenarioError(f"no scenario with id {only!r} in {directory}")
    return scenarios


# same setup as the db fixture in tests/conftest.py: one in-memory db shared by every session
@contextmanager
def temp_db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    try:
        yield sessionmaker(bind=engine, expire_on_commit=False)
    finally:
        engine.dispose()


def _cents(dollars: float) -> int:
    return round(dollars * 100)


def seed(session: Session, scenario: Scenario, today: date) -> str:
    user_id = f"eval-{scenario.id}"
    item = add_user(session, user_id, f"{scenario.id}@eval.local")
    accounts = {}
    for a in scenario.user["accounts"]:
        liability = None
        if "liability" in a:
            raw = a["liability"]
            liability = {"kind": raw["kind"], "apr": raw.get("apr")}
            if "min_payment" in raw:
                liability["min_payment_cents"] = _cents(raw["min_payment"])
            if "due_in_days" in raw:
                liability["next_due_date"] = today + timedelta(days=raw["due_in_days"])
        accounts[a["name"]] = add_account(session, item, a["name"], type_=a["type"], subtype=a.get("subtype"),
                                          balance=_cents(a["balance"]), liability=liability)
    for t in scenario.user.get("transactions", []):
        add_txn(session, accounts[t["account"]], _cents(t["amount"]), t["category"],
                merchant=t.get("merchant", "Shop"), day=today - timedelta(days=t["days_ago"]))
    session.commit()
    return user_id
