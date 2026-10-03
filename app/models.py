"""SQLAlchemy models. See README for the ER diagram and design notes."""
import enum
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


def _enum(cls: type[enum.Enum], length: int = 20) -> Enum:
    # VARCHAR + CHECK constraint instead of a native PG enum: trivially portable and
    # adding a value later doesn't need ALTER TYPE.
    return Enum(cls, native_enum=False, length=length, create_constraint=True,
                validate_strings=True)


class UserRole(str, enum.Enum):
    USER = "USER"
    ADMIN = "ADMIN"


class BookingStatus(str, enum.Enum):
    PENDING = "PENDING"
    CONFIRMED = "CONFIRMED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class PaymentStatus(str, enum.Enum):
    PENDING = "PENDING"
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"


class WebhookEventStatus(str, enum.Enum):
    RECEIVED = "RECEIVED"    # stored, not yet processed (or in flight)
    PROCESSED = "PROCESSED"  # applied to a payment/booking
    IGNORED = "IGNORED"      # valid but a no-op (already applied / conflicting)
    FAILED = "FAILED"        # processing error, eligible for retry


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow,
        server_default=func.now(), nullable=False,
    )


class User(TimestampMixin, Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)  # stored lowercase
    full_name: Mapped[str] = mapped_column(String(120))
    password_hash: Mapped[str] = mapped_column(String(128))
    role: Mapped[UserRole] = mapped_column(_enum(UserRole), default=UserRole.USER)


class DiagnosticTest(TimestampMixin, Base):
    __tablename__ = "diagnostic_tests"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(32), unique=True)
    name: Mapped[str] = mapped_column(String(160), unique=True)
    description: Mapped[str | None] = mapped_column(Text)


class Centre(TimestampMixin, Base):
    __tablename__ = "centres"
    __table_args__ = (UniqueConstraint("name", "city", name="uq_centre_name_city"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(160))
    address: Mapped[str] = mapped_column(String(300))
    city: Mapped[str] = mapped_column(String(80), index=True)
    latitude: Mapped[Decimal | None] = mapped_column(Numeric(9, 6))
    longitude: Mapped[Decimal | None] = mapped_column(Numeric(9, 6))

    centre_tests: Mapped[list["CentreTest"]] = relationship(
        back_populates="centre", cascade="all, delete-orphan", order_by="CentreTest.id"
    )

    @property
    def tests(self) -> list["CentreTest"]:
        return [ct for ct in self.centre_tests if ct.is_active]


class CentreTest(TimestampMixin, Base):
    """Join table carrying the price: the same test can cost differently per centre."""

    __tablename__ = "centre_tests"
    __table_args__ = (
        UniqueConstraint("centre_id", "test_id", name="uq_centre_test"),
        CheckConstraint("price > 0", name="ck_centre_test_price_positive"),
        Index("ix_centre_tests_test_id", "test_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    centre_id: Mapped[int] = mapped_column(ForeignKey("centres.id", ondelete="CASCADE"))
    test_id: Mapped[int] = mapped_column(ForeignKey("diagnostic_tests.id", ondelete="RESTRICT"))
    price: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"))

    centre: Mapped[Centre] = relationship(back_populates="centre_tests")
    test: Mapped[DiagnosticTest] = relationship(lazy="joined")

    @property
    def test_name(self) -> str:
        return self.test.name

    @property
    def test_code(self) -> str:
        return self.test.code


_ACTIVE = "status IN ('PENDING', 'CONFIRMED')"


class Booking(TimestampMixin, Base):
    __tablename__ = "bookings"
    __table_args__ = (
        # A booking can only reference a test that the centre actually offers.
        ForeignKeyConstraint(
            ["centre_id", "test_id"],
            ["centre_tests.centre_id", "centre_tests.test_id"],
            name="fk_booking_centre_test",
        ),
        CheckConstraint("amount > 0", name="ck_booking_amount_positive"),
        Index("ix_bookings_user_created", "user_id", "created_at"),
        Index("ix_bookings_status_created", "status", "created_at"),
        # Same user can't hold two live bookings for the same test/centre/slot.
        Index(
            "uq_booking_active_slot", "user_id", "centre_id", "test_id", "appointment_at",
            unique=True, postgresql_where=text(_ACTIVE), sqlite_where=text(_ACTIVE),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    centre_id: Mapped[int] = mapped_column(Integer)
    test_id: Mapped[int] = mapped_column(Integer)
    amount: Mapped[Decimal] = mapped_column(Numeric(10, 2))  # price snapshot at booking time
    appointment_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    status: Mapped[BookingStatus] = mapped_column(
        _enum(BookingStatus), default=BookingStatus.PENDING
    )

    centre: Mapped[Centre] = relationship(
        lazy="joined", foreign_keys=[centre_id],
        primaryjoin="Booking.centre_id == Centre.id",
    )
    test: Mapped[DiagnosticTest] = relationship(
        lazy="joined", foreign_keys=[test_id],
        primaryjoin="Booking.test_id == DiagnosticTest.id",
    )

    @property
    def centre_name(self) -> str:
        return self.centre.name

    @property
    def test_name(self) -> str:
        return self.test.name


_OPEN_PAYMENT = "status IN ('PENDING', 'SUCCESS')"


class Payment(TimestampMixin, Base):
    __tablename__ = "payments"
    __table_args__ = (
        UniqueConstraint("user_id", "idempotency_key", name="uq_payment_user_idem_key"),
        CheckConstraint("amount > 0", name="ck_payment_amount_positive"),
        # At most one in-flight-or-successful payment per booking => no double charging,
        # even under concurrent requests. FAILED attempts don't count, so retries work.
        Index(
            "uq_payment_open_per_booking", "booking_id", unique=True,
            postgresql_where=text(_OPEN_PAYMENT), sqlite_where=text(_OPEN_PAYMENT),
        ),
        Index("ix_payments_user_created", "user_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    booking_id: Mapped[int] = mapped_column(ForeignKey("bookings.id", ondelete="RESTRICT"))
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    amount: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    status: Mapped[PaymentStatus] = mapped_column(_enum(PaymentStatus))
    provider_ref: Mapped[str] = mapped_column(String(64), unique=True)  # id at the "provider"
    idempotency_key: Mapped[str | None] = mapped_column(String(128))
    failure_reason: Mapped[str | None] = mapped_column(String(255))


class WebhookEvent(Base):
    """Inbox table: one row per provider event_id. The UNIQUE(event_id) constraint is
    what makes webhook handling idempotent."""

    __tablename__ = "webhook_events"
    __table_args__ = (Index("ix_webhook_events_status", "status", "last_attempt_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    event_id: Mapped[str] = mapped_column(String(128), unique=True)
    event_type: Mapped[str] = mapped_column(String(64))
    payment_ref: Mapped[str] = mapped_column(String(64), index=True)
    payload: Mapped[dict] = mapped_column(JSON().with_variant(JSONB(), "postgresql"))
    status: Mapped[WebhookEventStatus] = mapped_column(
        _enum(WebhookEventStatus), default=WebhookEventStatus.RECEIVED
    )
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(String(500))
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, server_default=func.now()
    )
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
