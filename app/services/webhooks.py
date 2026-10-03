"""Idempotent payment-webhook processing (inbox pattern).

Flow
  1. Verify HMAC signature (done in the API layer on the raw body).
  2. INSERT the event keyed by UNIQUE(event_id). A duplicate delivery hits the unique
     constraint and is answered from the stored row - nothing is applied twice.
  3. Lock the event row (SELECT ... FOR UPDATE) so two concurrent deliveries of the
     same event serialise, re-check status, then apply the change in one transaction.
  4. On error the event is marked FAILED (attempts++). The provider's own retry (same
     event_id) or the Celery beat job re-runs it with exponential back-off.

Applying an event is also *state-idempotent*: it only changes a payment that is still
PENDING, so distinct events for the same payment can't corrupt a settled payment.
"""
import logging
import hmac
import hashlib
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.errors import AppError
from app.models import (
    Booking, Payment, PaymentStatus, WebhookEvent, WebhookEventStatus, utcnow,
)
from app.schemas import WebhookIn, WebhookOut
from app.services.bookings import sync_booking_with_payment

logger = logging.getLogger(__name__)

_DONE = (WebhookEventStatus.PROCESSED, WebhookEventStatus.IGNORED)


def sign(body: bytes, secret: str | None = None) -> str:
    key = (secret or get_settings().webhook_secret).encode()
    return hmac.new(key, body, hashlib.sha256).hexdigest()


def verify_signature(body: bytes, signature: str | None) -> bool:
    return bool(signature) and hmac.compare_digest(sign(body), signature.strip().lower())


def _result(event: WebhookEvent, result: str, payment: Payment | None = None,
            booking: Booking | None = None) -> WebhookOut:
    return WebhookOut(
        result=result,  # type: ignore[arg-type]
        event_id=event.event_id,
        payment_id=payment.id if payment else None,
        payment_status=payment.status if payment else None,
        booking_id=booking.id if booking else None,
        booking_status=booking.status if booking else None,
    )


def _current_state(db: Session, payment_ref: str) -> tuple[Payment | None, Booking | None]:
    payment = db.scalar(select(Payment).where(Payment.provider_ref == payment_ref))
    booking = db.get(Booking, payment.booking_id) if payment else None
    return payment, booking


def handle_webhook(db: Session, payload: WebhookIn) -> WebhookOut:
    event = WebhookEvent(
        event_id=payload.event_id,
        event_type=payload.event_type,
        payment_ref=payload.payment_ref,
        payload=payload.model_dump(mode="json"),
    )
    db.add(event)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        existing = db.scalar(select(WebhookEvent).where(WebhookEvent.event_id == payload.event_id))
        if existing is None:  # pragma: no cover - deleted between statements
            raise AppError(503, "retry_later", "Please retry")
        if existing.status in _DONE:
            logger.info("webhook_duplicate", extra={"event_id": payload.event_id})
            payment, booking = _current_state(db, existing.payment_ref)
            return _result(existing, "duplicate", payment, booking)
        event = existing  # RECEIVED / FAILED -> (re)process below
    return process_event(db, event.id)


def process_event(db: Session, event_pk: int) -> WebhookOut:
    event = db.scalar(
        select(WebhookEvent).where(WebhookEvent.id == event_pk).with_for_update()
        .execution_options(populate_existing=True)  # refresh rows loaded before we got the lock
    )
    if event is None:
        raise AppError(404, "event_not_found", "Webhook event not found")
    if event.status in _DONE:  # a concurrent delivery finished while we waited for the lock
        payment, booking = _current_state(db, event.payment_ref)
        db.rollback()
        return _result(event, "duplicate", payment, booking)

    try:
        payment = db.scalar(
            select(Payment).where(Payment.provider_ref == event.payment_ref).with_for_update()
            .execution_options(populate_existing=True)
        )
        if payment is None:
            raise AppError(404, "payment_not_found",
                           f"No payment with reference {event.payment_ref}")
        booking = db.scalar(select(Booking).where(Booking.id == payment.booking_id).with_for_update(of=Booking)
            .execution_options(populate_existing=True))
        new_status = PaymentStatus(event.payload["status"])

        if payment.status == PaymentStatus.PENDING:
            payment.status = new_status
            if new_status == PaymentStatus.FAILED:
                payment.failure_reason = "Reported failed by payment provider"
            sync_booking_with_payment(booking, new_status)
            event.status, outcome = WebhookEventStatus.PROCESSED, "processed"
        else:
            if payment.status != new_status:
                logger.warning("webhook_conflicts_with_settled_payment", extra={
                    "event_id": event.event_id, "payment_id": payment.id,
                    "current": payment.status.value, "reported": new_status.value,
                })
            event.status, outcome = WebhookEventStatus.IGNORED, "ignored"

        event.attempts += 1
        event.last_attempt_at = event.processed_at = utcnow()
        event.last_error = None
        db.commit()
        logger.info("webhook_processed", extra={"event_id": event.event_id, "result": outcome})
        return _result(event, outcome, payment, booking)
    except Exception as exc:
        db.rollback()
        _mark_failed(db, event_pk, exc)
        raise


def _mark_failed(db: Session, event_pk: int, exc: Exception) -> None:
    event = db.get(WebhookEvent, event_pk)
    if event is None:  # pragma: no cover
        return
    event.status = WebhookEventStatus.FAILED
    event.attempts += 1
    event.last_attempt_at = utcnow()
    event.last_error = str(exc)[:500]
    db.commit()
    logger.warning("webhook_failed", extra={
        "event_id": event.event_id, "attempts": event.attempts, "error": event.last_error,
    })


def retry_failed_events(db: Session, now: datetime | None = None, batch: int = 100) -> int:
    """Re-run FAILED events whose exponential back-off (10s * 2^attempts) has elapsed and
    that haven't exhausted WEBHOOK_MAX_ATTEMPTS. Returns how many were retried."""
    now = now or utcnow()
    max_attempts = get_settings().webhook_max_attempts
    candidates = db.scalars(
        select(WebhookEvent)
        .where(WebhookEvent.status == WebhookEventStatus.FAILED,
               WebhookEvent.attempts < max_attempts)
        .order_by(WebhookEvent.id).limit(batch)
    ).all()
    retried = 0
    for ev in candidates:
        last = ev.last_attempt_at or ev.received_at
        if last.tzinfo is None:  # SQLite returns naive datetimes
            last = last.replace(tzinfo=now.tzinfo)
        if now - last < timedelta(seconds=10 * 2 ** ev.attempts):
            continue
        retried += 1
        try:
            process_event(db, ev.id)
        except Exception:
            pass  # already recorded on the event; next beat tick retries
    return retried
