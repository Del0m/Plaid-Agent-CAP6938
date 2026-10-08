import logging
from typing import Union
import httpx
from fastapi import BackgroundTasks
from sqlalchemy import select
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.orm import Session
from ..db.models import PlaidItem
from .client import PlaidError
from .sync import sync_transactions

# plaid -> us notifications. plaid retries slow or failed deliveries,
# so answer fast and do the real work after the response is sent.

logger = logging.getLogger(__name__)


# runs after the webhook response is sent. the request's session is closed by then,
# so open a new one on the same engine (in tests that's the test db, not dev.db)
def sync_in_background(bind: Union[Engine, Connection], item_id: int) -> None:
    with Session(bind=bind, expire_on_commit=False) as session:
        item = session.get(PlaidItem, item_id)
        if item is None:
            return
        try:
            sync_transactions(session, item)
        except (PlaidError, httpx.HTTPError) as err:
            logger.warning("webhook sync failed for item %s: %s", item_id, type(err).__name__)


# SYNC_UPDATES_AVAILABLE means new transaction pages are waiting behind our cursor.
# everything else is logged and ignored; the response is always 200 so plaid stops retrying.
def handle_webhook(payload: dict, session: Session, background: BackgroundTasks) -> None:
    webhook_type, code = payload.get("webhook_type"), payload.get("webhook_code")
    if (webhook_type, code) != ("TRANSACTIONS", "SYNC_UPDATES_AVAILABLE"):
        logger.info("ignoring webhook %s/%s", webhook_type, code)
        return

    item = session.scalar(select(PlaidItem).where(PlaidItem.plaid_item_id == payload.get("item_id")))
    if item is None:
        logger.warning("webhook for unknown item")
        return
    background.add_task(sync_in_background, session.get_bind(), item.id)
