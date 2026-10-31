import sqlite3
from functools import lru_cache

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.sqlite import SqliteSaver

from .. import config
from ..agents.llm import get_chat_model
from ..db.session import SessionLocal

# what the budget and alerts routes run against. the routes read these as budget_hooks.<name> at call time,
# so tests swap the db, the model and the checkpointer in one place

session_factory = SessionLocal
make_model = get_chat_model


@lru_cache(maxsize=1)
def _sqlite_saver() -> SqliteSaver:
    config.BUDGET_CHECKPOINT_PATH.parent.mkdir(parents=True, exist_ok=True)
    return SqliteSaver(sqlite3.connect(str(config.BUDGET_CHECKPOINT_PATH), check_same_thread=False))


def get_checkpointer() -> BaseCheckpointSaver:
    return _sqlite_saver()
