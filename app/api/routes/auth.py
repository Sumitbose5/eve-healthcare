from fastapi import APIRouter, Depends, status
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy.orm import Session

from app.api.deps import get_current_user
from app.core.rate_limit import auth_rate_limit
from app.core.security import create_access_token
from app.db.session import get_db
from app.models import User
from app.schemas import LoginIn, SignupIn, TokenOut, UserOut
from app.services import auth as auth_service

router = APIRouter(prefix="/auth", tags=["Auth"])


def _token(user: User) -> TokenOut:
    token, expires_in = create_access_token(user.id, user.role.value)
    return TokenOut(access_token=token, expires_in=expires_in)


@router.post("/signup", response_model=UserOut, status_code=status.HTTP_201_CREATED,
             dependencies=[Depends(auth_rate_limit)])
def signup(data: SignupIn, db: Session = Depends(get_db)):
    """Create an account. Emails listed in `ADMIN_EMAILS` become admins."""
    return auth_service.signup(db, data)


@router.post("/login", response_model=TokenOut, dependencies=[Depends(auth_rate_limit)])
def login(data: LoginIn, db: Session = Depends(get_db)):
    """Exchange email + password for a JWT access token."""
    return _token(auth_service.authenticate(db, data.email, data.password))


@router.post("/token", response_model=TokenOut, include_in_schema=True,
             dependencies=[Depends(auth_rate_limit)])
def login_form(form: OAuth2PasswordRequestForm = Depends(), db: Session = Depends(get_db)):
    """OAuth2 password-flow form login (`username` = email). Powers Swagger's *Authorize* button."""
    return _token(auth_service.authenticate(db, form.username, form.password))


@router.get("/me", response_model=UserOut)
def me(user: User = Depends(get_current_user)):
    return user
