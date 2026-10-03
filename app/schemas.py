"""Pydantic request/response schemas."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Generic, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

from app.models import BookingStatus, PaymentStatus, UserRole

T = TypeVar("T")


class ORM(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class Page(BaseModel, Generic[T]):
    items: list[T]
    total: int
    limit: int
    offset: int


# ---- auth -------------------------------------------------------------------
class SignupIn(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=72)
    full_name: str = Field(min_length=1, max_length=120)

    @field_validator("password")
    @classmethod
    def _strong(cls, v: str) -> str:
        if len(v.encode()) > 72:
            raise ValueError("password must be at most 72 bytes")
        if not (any(c.isalpha() for c in v) and any(c.isdigit() for c in v)):
            raise ValueError("password must contain at least one letter and one digit")
        return v

    @field_validator("full_name")
    @classmethod
    def _strip(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("full_name must not be blank")
        return v


class LoginIn(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=72)


class UserOut(ORM):
    id: int
    email: EmailStr
    full_name: str
    role: UserRole
    created_at: datetime


class TokenOut(BaseModel):
    access_token: str
    token_type: Literal["bearer"] = "bearer"
    expires_in: int


# ---- catalogue --------------------------------------------------------------
class TestCreate(BaseModel):
    __test__ = False  # not a pytest class
    code: str = Field(min_length=2, max_length=32, pattern=r"^[A-Za-z0-9_-]+$")
    name: str = Field(min_length=2, max_length=160)
    description: str | None = Field(default=None, max_length=2000)


class TestOut(ORM):
    __test__ = False
    id: int
    code: str
    name: str
    description: str | None


class CentreCreate(BaseModel):
    name: str = Field(min_length=2, max_length=160)
    address: str = Field(min_length=2, max_length=300)
    city: str = Field(min_length=2, max_length=80)
    latitude: Decimal | None = Field(default=None, ge=-90, le=90)
    longitude: Decimal | None = Field(default=None, ge=-180, le=180)


class CentreTestPrice(BaseModel):
    price: Decimal = Field(gt=0, max_digits=10, decimal_places=2)


class CentreTestOut(ORM):
    test_id: int
    test_code: str
    test_name: str
    price: Decimal


class CentreOut(ORM):
    id: int
    name: str
    address: str
    city: str
    latitude: Decimal | None
    longitude: Decimal | None
    tests: list[CentreTestOut]


# ---- bookings ---------------------------------------------------------------
class BookingCreate(BaseModel):
    centre_id: int = Field(gt=0)
    test_id: int = Field(gt=0)
    appointment_at: datetime

    @field_validator("appointment_at")
    @classmethod
    def _valid_slot(cls, v: datetime) -> datetime:
        if v.tzinfo is None or v.utcoffset() is None:
            raise ValueError(
                "appointment_at must include a timezone offset, e.g. 2026-10-05T10:30:00+05:30"
            )
        now = datetime.now(timezone.utc)
        if v < now + timedelta(minutes=30):
            raise ValueError("appointment_at must be at least 30 minutes in the future")
        if v > now + timedelta(days=180):
            raise ValueError("appointment_at cannot be more than 180 days ahead")
        return v


class BookingOut(ORM):
    id: int
    user_id: int
    centre_id: int
    centre_name: str
    test_id: int
    test_name: str
    amount: Decimal
    appointment_at: datetime
    status: BookingStatus
    created_at: datetime
    updated_at: datetime


# ---- payments ---------------------------------------------------------------
class PaymentCreate(BaseModel):
    booking_id: int = Field(gt=0)
    simulate_outcome: Literal["SUCCESS", "FAILED", "PENDING"] | None = Field(
        default=None,
        description="Force the simulated result (testing aid). PENDING leaves the payment "
        "open until a webhook arrives. Omit for a random outcome.",
    )


class PaymentOut(ORM):
    id: int
    booking_id: int
    amount: Decimal
    status: PaymentStatus
    provider_ref: str
    failure_reason: str | None
    created_at: datetime
    updated_at: datetime


class WebhookIn(BaseModel):
    event_id: str = Field(min_length=1, max_length=128)
    event_type: str = Field(default="payment.status_updated", max_length=64)
    payment_ref: str = Field(min_length=1, max_length=64)
    status: Literal["SUCCESS", "FAILED"]
    occurred_at: datetime | None = None


class WebhookOut(BaseModel):
    result: Literal["processed", "ignored", "duplicate"]
    event_id: str
    payment_id: int | None = None
    payment_status: PaymentStatus | None = None
    booking_id: int | None = None
    booking_status: BookingStatus | None = None
