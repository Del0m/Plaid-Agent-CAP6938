import logging
import httpx
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from ..db.models import PlaidItem, User
from ..db.session import get_session
from ..plaid_service import client as plaid
from ..plaid_service.client import PlaidError
from ..plaid_service.sync import sync_item
from ..plaid_service.webhooks import handle_webhook
from ..security import encrypt
from .deps import get_current_user

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/plaid", tags=["plaid"])


class LinkTokenResponse(BaseModel):
    link_token: str

class ExchangeRequest(BaseModel):
    public_token: str

class ExchangeResponse(BaseModel):
    item_id: int


# turn plaid/network failures into an HTTP error without leaking secrets or tokens
def _plaid_failure(err: Exception) -> HTTPException:
    if isinstance(err, PlaidError):
        logger.warning("plaid error %s (HTTP %s)", err.error_code, err.status_code)
        # plaid 400 = bad input from our caller (e.g. expired public_token)
        code = status.HTTP_400_BAD_REQUEST if err.status_code == 400 else status.HTTP_502_BAD_GATEWAY
        return HTTPException(code, detail={"plaid_error_code": err.error_code})
    logger.warning("plaid unreachable: %s", type(err).__name__)
    return HTTPException(status.HTTP_502_BAD_GATEWAY, detail="plaid unreachable")


@router.post("/link-token", response_model=LinkTokenResponse)
def create_link_token(user: User = Depends(get_current_user)):
    try:
        token = plaid.create_link_token(client_user_id=user.id)
    except (PlaidError, httpx.HTTPError) as err:
        raise _plaid_failure(err)
    return LinkTokenResponse(link_token=token)


# store the item with its access_token encrypted; the token is never returned or logged
@router.post("/exchange", response_model=ExchangeResponse)
def exchange_public_token(
    body: ExchangeRequest,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    try:
        access_token, plaid_item_id = plaid.exchange_public_token(body.public_token)
    except (PlaidError, httpx.HTTPError) as err:
        raise _plaid_failure(err)

    # item already stored (Link re-run for a connected bank): keep the newest token on
    # the same row, but never touch another user's item
    existing = session.scalar(select(PlaidItem).where(PlaidItem.plaid_item_id == plaid_item_id))
    if existing is not None:
        if existing.user_id != user.id:
            raise HTTPException(status.HTTP_409_CONFLICT, detail="item already linked")
        existing.access_token_enc = encrypt(access_token)
        existing.status = "active"
        session.commit()
        return ExchangeResponse(item_id=existing.id)

    item = PlaidItem(
        user_id=user.id,
        plaid_item_id=plaid_item_id,
        access_token_enc=encrypt(access_token),
    )
    session.add(item)
    try:
        session.commit()
    except IntegrityError:
        # a concurrent exchange inserted the same item between our check and commit
        session.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, detail="item already linked")
    return ExchangeResponse(item_id=item.id)


# pull everything for one of the current user's items; safe to call repeatedly
@router.post("/items/{item_id}/sync")
def sync_one_item(
    item_id: int,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    item = session.get(PlaidItem, item_id)
    # someone else's item looks the same as a missing one, so ids can't be probed
    if item is None or item.user_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="item not found")
    try:
        return sync_item(session, item)
    except (PlaidError, httpx.HTTPError) as err:
        raise _plaid_failure(err)


# plaid calls this, not a user, so there's no get_current_user.
# TODO: verify the Plaid-Verification JWT before trusting the payload
@router.post("/webhook")
def plaid_webhook(
    payload: dict,
    background: BackgroundTasks,
    session: Session = Depends(get_session),
):
    handle_webhook(payload, session, background)
    return {"status": "ok"}
