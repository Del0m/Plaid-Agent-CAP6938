import os

from cryptography.fernet import Fernet

# app.security fails at import without a key; a throwaway one is fine for tests.
# setdefault so a real .env key still wins (load_dotenv never overrides set vars)
os.environ.setdefault("FERNET_KEY", Fernet.generate_key().decode())

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.base import Base
from app.db.session import get_session
from app.main import app


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
