from dataclasses import dataclass

import jwt
from fastapi import Depends, Query
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.orm import Session

from app.core.errors import AppError
from app.core.security import decode_access_token
from app.db.session import get_db
from app.models import User, UserRole

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/token", auto_error=False)

_UNAUTH_HEADERS = {"WWW-Authenticate": "Bearer"}


def get_current_user(
    token: str | None = Depends(oauth2_scheme), db: Session = Depends(get_db)
) -> User:
    if not token:
        raise AppError(401, "not_authenticated", "Not authenticated", _UNAUTH_HEADERS)
    try:
        claims = decode_access_token(token)
        user_id = int(claims["sub"])
    except (jwt.PyJWTError, ValueError, KeyError):
        raise AppError(401, "invalid_token", "Invalid or expired token", _UNAUTH_HEADERS)
    user = db.get(User, user_id)
    if user is None:
        raise AppError(401, "invalid_token", "Invalid or expired token", _UNAUTH_HEADERS)
    return user


def require_admin(user: User = Depends(get_current_user)) -> User:
    if user.role != UserRole.ADMIN:
        raise AppError(403, "forbidden", "Admin privileges required")
    return user


@dataclass
class Pagination:
    limit: int
    offset: int


def pagination(
    limit: int = Query(20, ge=1, le=100, description="Page size (max 100)"),
    offset: int = Query(0, ge=0, description="Items to skip"),
) -> Pagination:
    return Pagination(limit, offset)
