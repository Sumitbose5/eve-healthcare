import logging
import random
import uuid

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.errors import AppError
from app.models import BookingStatus, Payment, PaymentStatus, User, UserRole
from app.schemas import PaymentCreate
from app.services.bookings import get_booking, sync_booking_with_payment

logger = logging.getLogger(__name__)

_FAILURE_REASONS = ["Insufficient funds", "Card declined", "Bank timeout", "3-D Secure failed"]


def _simulate_outcome(requested: str | None) -> PaymentStatus:
    settings = get_settings()
    if requested and settings.allow_simulated_outcome:
        return PaymentStatus(requested)
    ok = random.random() < settings.payment_success_rate
    return PaymentStatus.SUCCESS if ok else PaymentStatus.FAILED


def initiate_payment(
    db: Session, user: User, data: PaymentCreate, idempotency_key: str | None
) -> tuple[Payment, bool]:
    """Simulate a payment for a booking. Returns (payment, created).

    * Same Idempotency-Key from the same user replays the original result.
    * Only one PENDING/SUCCESS payment may exist per booking (also DB-enforced).
    * The amount always comes from the booking, never from the client.
    """
    if idempotency_key:
        existing = db.scalar(
            select(Payment).where(
                Payment.user_id == user.id, Payment.idempotency_key == idempotency_key
            )
        )
        if existing:
            if existing.booking_id != data.booking_id:
                raise AppError(409, "idempotency_key_reused",
                               "Idempotency-Key was already used for a different booking")
            return existing, False

    # Lock the booking row so concurrent payment attempts serialise here.
    booking = get_booking(db, user, data.booking_id, lock=True)
    if booking.user_id != user.id:
        # admins can read others' bookings but not pay for them
        raise AppError(404, "booking_not_found", "Booking not found")
    if booking.status == BookingStatus.CONFIRMED:
        raise AppError(409, "already_paid", "Booking is already confirmed and paid")
    if booking.status == BookingStatus.CANCELLED:
        raise AppError(409, "booking_cancelled", "Cannot pay for a cancelled booking")

    open_payment = db.scalar(
        select(Payment.id).where(
            Payment.booking_id == booking.id,
            Payment.status.in_([PaymentStatus.PENDING, PaymentStatus.SUCCESS]),
        )
    )
    if open_payment:
        raise AppError(409, "payment_in_progress",
                       "A payment for this booking is already pending or completed")

    outcome = _simulate_outcome(data.simulate_outcome)
    payment = Payment(
        booking_id=booking.id,
        user_id=user.id,
        amount=booking.amount,
        status=outcome,
        provider_ref=f"pay_{uuid.uuid4().hex[:24]}",
        idempotency_key=idempotency_key,
        failure_reason=random.choice(_FAILURE_REASONS) if outcome == PaymentStatus.FAILED else None,
    )
    db.add(payment)
    sync_booking_with_payment(booking, outcome)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise AppError(409, "payment_in_progress",
                       "A payment for this booking is already pending or completed")
    logger.info("payment_processed", extra={
        "payment_id": payment.id, "booking_id": booking.id,
        "status": outcome.value, "booking_status": booking.status.value,
    })
    return payment, True


def get_payment(db: Session, user: User, payment_id: int) -> Payment:
    stmt = select(Payment).where(Payment.id == payment_id)
    if user.role != UserRole.ADMIN:
        stmt = stmt.where(Payment.user_id == user.id)
    payment = db.scalar(stmt)
    if payment is None:
        raise AppError(404, "payment_not_found", "Payment not found")
    return payment


def list_payments(
    db: Session, user: User, *, booking_id: int | None, limit: int, offset: int
) -> tuple[list[Payment], int]:
    stmt = select(Payment).where(Payment.user_id == user.id)
    if booking_id:
        stmt = stmt.where(Payment.booking_id == booking_id)
    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = db.scalars(
        stmt.order_by(Payment.created_at.desc(), Payment.id.desc()).limit(limit).offset(offset)
    ).all()
    return list(rows), total
