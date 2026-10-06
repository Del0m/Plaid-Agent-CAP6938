import uuid
from fastapi import Depends
from sqlalchemy import select
from sqlalchemy.orm import Session
from ..db.models import User
from ..db.session import get_session

DEV_USER_EMAIL = "dev@localhost"

# stand-in until the auth ticket: every request is the one dev user, created on first use.
# replace this function with real auth; routes don't need to change.
def get_current_user(session: Session = Depends(get_session)) -> User:
    user = session.scalar(select(User).where(User.email == DEV_USER_EMAIL))
    if user is None:
        # "!" is not a valid hash, so nobody can log in as this user
        user = User(id=str(uuid.uuid4()), email=DEV_USER_EMAIL, password_hash="!")
        session.add(user)
        session.commit()
    return user
