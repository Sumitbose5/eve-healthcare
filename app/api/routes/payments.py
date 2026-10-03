import json

from fastapi import APIRouter, Depends, Header, Query, Request, Response, status
from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.api.deps import Pagination, get_current_user, pagination
from app.core.errors import AppError
from app.core.rate_limit import payment_rate_limit
from app.db.session import get_db
from app.models import User
from app.schemas import Page, PaymentCreate, PaymentOut, WebhookIn, WebhookOut
from app.services import payments as payment_service
from app.services import webhooks as webhook_service

router = APIRouter(prefix="/payments", tags=["Payments"])


async def verified_webhook_body(
    request: Request,
    signature: str | None = Header(None, alias="X-Webhook-Signature",
                                   description="hex HMAC-SHA256 of the raw request body"),
) -> bytes:
    body = await request.body()
    if not webhook_service.verify_signature(body, signature):
        raise AppError(401, "invalid_signature", "Missing or invalid webhook signature")
    return body


@router.post("/webhook/", response_model=WebhookOut, tags=["Webhooks"])
def payment_webhook(body: bytes = Depends(verified_webhook_body), db: Session = Depends(get_db)):
    """Receive a payment-status update from the (simulated) provider.

    **Idempotent**: keyed on `event_id`. Redelivering an event returns `result: duplicate`
    and changes nothing. Unknown `payment_ref` returns 404 and the event is retried
    (provider redelivery or the background job). Sign the raw body with
    HMAC-SHA256(`WEBHOOK_SECRET`) and send it in `X-Webhook-Signature`.
    """
    try:
        payload = WebhookIn.model_validate_json(body)
    except ValidationError as exc:
        raise AppError(422, "invalid_payload",
                       json.loads(exc.json(include_url=False, include_context=False)))
    return webhook_service.handle_webhook(db, payload)


@router.post("/", response_model=PaymentOut, status_code=status.HTTP_201_CREATED,
             dependencies=[Depends(payment_rate_limit)])
def create_payment(
    data: PaymentCreate,
    response: Response,
    idempotency_key: str | None = Header(None, alias="Idempotency-Key", max_length=128),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Simulate paying for one of your bookings. Result is SUCCESS or FAILED (or PENDING when
    `simulate_outcome=PENDING`, to be settled by webhook) and the booking is updated
    accordingly. Send an `Idempotency-Key` header to make client retries safe."""
    payment, created = payment_service.initiate_payment(db, user, data, idempotency_key)
    if not created:
        response.status_code = status.HTTP_200_OK
    return payment


@router.get("/", response_model=Page[PaymentOut])
def list_my_payments(
    booking_id: int | None = Query(None, gt=0),
    page: Pagination = Depends(pagination),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    rows, total = payment_service.list_payments(
        db, user, booking_id=booking_id, limit=page.limit, offset=page.offset)
    return Page[PaymentOut](items=rows, total=total, limit=page.limit, offset=page.offset)


@router.get("/{payment_id}", response_model=PaymentOut)
def get_payment(payment_id: int, user: User = Depends(get_current_user),
                db: Session = Depends(get_db)):
    return payment_service.get_payment(db, user, payment_id)
