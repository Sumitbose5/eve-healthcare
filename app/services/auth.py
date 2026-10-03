import logging

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.errors import AppError
from app.core.security import hash_password, verify_password
from app.models import User, UserRole
from app.schemas import SignupIn

logger = logging.getLogger(__name__)

_dummy_hash: str | None = None


def signup(db: Session, data: SignupIn) -> User:
    email = data.email.lower()
    if db.scalar(select(User.id).where(User.email == email)):
        raise AppError(409, "email_taken", "An account with this email already exists")
    role = UserRole.ADMIN if email in get_settings().admin_email_set else UserRole.USER
    user = User(
        email=email,
        full_name=data.full_name,
        password_hash=hash_password(data.password),
        role=role,
    )
    db.add(user)
    try:
        db.commit()
    except IntegrityError:  # lost a race with a concurrent signup
        db.rollback()
        raise AppError(409, "email_taken", "An account with this email already exists")
    logger.info("user_signed_up", extra={"user_id": user.id, "role": role.value})
    return user


def authenticate(db: Session, email: str, password: str) -> User:
    global _dummy_hash
    user = db.scalar(select(User).where(User.email == email.lower()))
    if user is None:
        # Hash anyway so response time doesn't reveal whether the email exists.
        _dummy_hash = _dummy_hash or hash_password("dummy-password-1")
        verify_password(password, _dummy_hash)
    elif verify_password(password, user.password_hash):
        return user
    logger.info("login_failed")
    raise AppError(401, "invalid_credentials", "Incorrect email or password",
                   headers={"WWW-Authenticate": "Bearer"})
