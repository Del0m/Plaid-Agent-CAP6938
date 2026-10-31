import os
import sys
from pathlib import Path

from cryptography.fernet import Fernet

# the eval harness (repo-root eval/) is tested from here; put the repo root on the path for it
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

# app.security fails at import without a key; a throwaway one is fine for tests.
# setdefault uses a fake throwaway key
os.environ.setdefault("FERNET_KEY", Fernet.generate_key().decode())

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.base import Base
from app.db.session import get_session
from app.main import app


# every test gets its own checkpoint files: code that opens config's path (the budget cli, the routes' default
# hooks) must never read or delete threads in the developer's backend/data/*.db
@pytest.fixture(autouse=True)
def checkpoint_paths(tmp_path, monkeypatch):
    from app import config
    from app.api import budget_hooks, chat
    monkeypatch.setattr(config, "BUDGET_CHECKPOINT_PATH", tmp_path / "budget_checkpoints.db")
    monkeypatch.setattr(config, "CHAT_CHECKPOINT_PATH", tmp_path / "checkpoints.db")
    budget_hooks._sqlite_saver.cache_clear()
    chat._sqlite_saver.cache_clear()
    yield
    budget_hooks._sqlite_saver.cache_clear()
    chat._sqlite_saver.cache_clear()


# fresh in-memory db per test; StaticPool keeps one connection so every session sees the same db
@pytest.fixture
def db():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
    yield SessionLocal
    engine.dispose()


# API client whose requests use the test db instead of dev.db
@pytest.fixture
def client(db):
    def override_get_session():
        with db() as session:
            yield session

    app.dependency_overrides[get_session] = override_get_session
    yield TestClient(app)
    app.dependency_overrides.clear()
