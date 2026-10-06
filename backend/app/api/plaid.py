import logging
import httpx
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.orm import Session
from ..db.models import PlaidItem, User
from ..db.session import get_session
from ..plaid_service import client as plaid
from ..plaid_service.client import PlaidError
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

    item = PlaidItem(
        user_id=user.id,
        plaid_item_id=plaid_item_id,
        access_token_enc=encrypt(access_token),
    )
    session.add(item)
    session.commit()
    return ExchangeResponse(item_id=item.id)
