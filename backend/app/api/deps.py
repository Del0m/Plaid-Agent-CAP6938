import uuid
from fastapi import Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session
from .. import config
from ..db.models import User
from ..db.session import get_session

DEV_USER_EMAIL = "dev@localhost"     # the default; config.DEV_USER_EMAIL can point at another user

# stand-in until the auth ticket: every request is one configured user (config.DEV_USER_EMAIL, read per request).
# only the default dev user is created on first use, so a typo in the setting can't make a stray user.
# replace this function with real auth; routes don't need to change.
def get_current_user(session: Session = Depends(get_session)) -> User:
    email = config.DEV_USER_EMAIL
    user = session.scalar(select(User).where(User.email == email))
    if user is None and email != DEV_USER_EMAIL:
        raise HTTPException(404, "Unknown DEV_USER_EMAIL user.")
    if user is None:
        # "!" is not a valid hash, so nobody can log in as this user
        user = User(id=str(uuid.uuid4()), email=DEV_USER_EMAIL, password_hash="!")
        session.add(user)
        session.commit()
    return user
