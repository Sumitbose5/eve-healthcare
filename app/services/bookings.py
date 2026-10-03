import logging
from datetime import datetime, timedelta

from sqlalchemy import exists, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.errors import AppError
from app.models import (
    Booking, BookingStatus, CentreTest, Payment, PaymentStatus, User, UserRole, utcnow,
)
from app.schemas import BookingCreate

logger = logging.getLogger(__name__)

_LIVE = (BookingStatus.PENDING, BookingStatus.CONFIRMED)


def create_booking(db: Session, user: User, data: BookingCreate) -> Booking:
    offered = db.scalar(
        select(CentreTest).where(
            CentreTest.centre_id == data.centre_id,
            CentreTest.test_id == data.test_id,
            CentreTest.is_active.is_(True),
        )
    )
    if offered is None:
        raise AppError(404, "test_not_offered",
                       "This centre does not offer the requested test")

    duplicate = db.scalar(
        select(Booking.id).where(
            Booking.user_id == user.id,
            Booking.centre_id == data.centre_id,
            Booking.test_id == data.test_id,
            Booking.appointment_at == data.appointment_at,
            Booking.status.in_(_LIVE),
        )
    )
    if duplicate:
        raise AppError(409, "duplicate_booking",
                       "You already have a booking for this test, centre and time")

    booking = Booking(
        user_id=user.id,
        centre_id=data.centre_id,
        test_id=data.test_id,
        amount=offered.price,  # snapshot: later price changes don't affect this booking
        appointment_at=data.appointment_at,
    )
    db.add(booking)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise AppError(409, "duplicate_booking",
                       "You already have a booking for this test, centre and time")
    logger.info("booking_created", extra={"booking_id": booking.id, "user_id": user.id})
    return booking


def get_booking(db: Session, user: User, booking_id: int, *, lock: bool = False) -> Booking:
    """Fetch a booking the caller may access. Non-owners get 404 (not 403) so booking
    ids can't be enumerated."""
    stmt = select(Booking).where(Booking.id == booking_id)
    if user.role != UserRole.ADMIN:
        stmt = stmt.where(Booking.user_id == user.id)
    if lock:
        stmt = stmt.with_for_update(of=Booking).execution_options(populate_existing=True)
    booking = db.scalar(stmt)
    if booking is None:
        raise AppError(404, "booking_not_found", "Booking not found")
    return booking


def list_bookings(
    db: Session, user: User, *, status: BookingStatus | None, limit: int, offset: int
) -> tuple[list[Booking], int]:
    stmt = select(Booking).where(Booking.user_id == user.id)
    if status:
        stmt = stmt.where(Booking.status == status)
    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = db.scalars(
        stmt.order_by(Booking.created_at.desc(), Booking.id.desc()).limit(limit).offset(offset)
    ).unique().all()
    return list(rows), total


def cancel_booking(db: Session, user: User, booking_id: int) -> Booking:
    booking = get_booking(db, user, booking_id, lock=True)
    if booking.status == BookingStatus.CANCELLED:
        raise AppError(409, "already_cancelled", "Booking is already cancelled")
    previous = booking.status
    booking.status = BookingStatus.CANCELLED
    db.commit()
    logger.info("booking_cancelled", extra={
        "booking_id": booking.id, "previous_status": previous.value,
        "refund_required": previous == BookingStatus.CONFIRMED,
    })
    return booking


def sync_booking_with_payment(booking: Booking, payment_status: PaymentStatus) -> None:
    """Single source of truth for how a payment outcome moves a booking.

      SUCCESS: PENDING|FAILED -> CONFIRMED   (CONFIRMED stays; CANCELLED stays + flagged)
      FAILED : PENDING        -> FAILED      (never downgrades CONFIRMED/CANCELLED)
    """
    if payment_status == PaymentStatus.SUCCESS:
        if booking.status in (BookingStatus.PENDING, BookingStatus.FAILED):
            booking.status = BookingStatus.CONFIRMED
        elif booking.status == BookingStatus.CANCELLED:
            logger.warning("payment_succeeded_for_cancelled_booking",
                           extra={"booking_id": booking.id, "refund_required": True})
    elif payment_status == PaymentStatus.FAILED:
        if booking.status == BookingStatus.PENDING:
            booking.status = BookingStatus.FAILED


def expire_stale_pending(db: Session, ttl_minutes: int, now: datetime | None = None) -> int:
    """Cancel PENDING bookings older than the TTL that have no payment in flight."""
    cutoff = (now or utcnow()) - timedelta(minutes=ttl_minutes)
    in_flight = exists().where(
        Payment.booking_id == Booking.id, Payment.status == PaymentStatus.PENDING
    )
    result = db.execute(
        update(Booking)
        .where(Booking.status == BookingStatus.PENDING, Booking.created_at < cutoff, ~in_flight)
        .values(status=BookingStatus.CANCELLED, updated_at=utcnow())
        .execution_options(synchronize_session=False)
    )
    db.commit()
    count = result.rowcount or 0
    if count:
        logger.info("stale_bookings_expired", extra={"count": count})
    return count
