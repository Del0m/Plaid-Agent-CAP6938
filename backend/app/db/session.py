import os
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

# relative sqlite path, so run the app and alembic from backend/
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./dev.db")

engine = create_engine(DATABASE_URL)

# sqlite ignores foreign keys unless turned on for every connection
@event.listens_for(engine, "connect")
def _enable_sqlite_fks(dbapi_conn, _):
    if DATABASE_URL.startswith("sqlite"):
        dbapi_conn.execute("PRAGMA foreign_keys=ON")

SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)

# one session per request, use with FastAPI Depends(get_session)
def get_session():
    with SessionLocal() as session:
        yield session
