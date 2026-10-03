from sqlalchemy import func, select

from app.models import Payment, WebhookEvent, WebhookEventStatus
from app.services.webhooks import retry_failed_events
from tests.conftest import webhook


def pending_payment(client, alice, booking):
    return client.post("/payments/", headers=alice, json={
        "booking_id": booking["id"], "simulate_outcome": "PENDING"}).json()


def booking_status(client, alice, booking):
    return client.get(f"/bookings/{booking['id']}", headers=alice).json()["status"]


def evt(ref, status="SUCCESS", event_id="evt_1"):
    return {"event_id": event_id, "payment_ref": ref, "status": status}


def count(db, model):
    return db.scalar(select(func.count()).select_from(model))


def test_signature_required(client, alice, booking):
    p = pending_payment(client, alice, booking)
    assert webhook(client, evt(p["provider_ref"]), signature=None).status_code == 401
    assert webhook(client, evt(p["provider_ref"]), signature="deadbeef").status_code == 401
    assert booking_status(client, alice, booking) == "PENDING"


def test_success_webhook_confirms_booking(client, alice, booking):
    p = pending_payment(client, alice, booking)
    r = webhook(client, evt(p["provider_ref"]))
    assert r.status_code == 200
    assert r.json() | {} == {"result": "processed", "event_id": "evt_1", "payment_id": p["id"],
                             "payment_status": "SUCCESS", "booking_id": booking["id"],
                             "booking_status": "CONFIRMED"}
    assert booking_status(client, alice, booking) == "CONFIRMED"


def test_failed_webhook_fails_booking(client, alice, booking):
    p = pending_payment(client, alice, booking)
    assert webhook(client, evt(p["provider_ref"], "FAILED")).json()["booking_status"] == "FAILED"
    # ...and the user may pay again afterwards
    r = client.post("/payments/", headers=alice, json={"booking_id": booking["id"],
                                                        "simulate_outcome": "SUCCESS"})
    assert r.status_code == 201 and booking_status(client, alice, booking) == "CONFIRMED"


def test_duplicate_event_is_idempotent(client, alice, booking, db):
    p = pending_payment(client, alice, booking)
    results = [webhook(client, evt(p["provider_ref"])).json()["result"] for _ in range(5)]
    assert results == ["processed"] + ["duplicate"] * 4
    assert count(db, Payment) == 1
    assert count(db, WebhookEvent) == 1
    assert booking_status(client, alice, booking) == "CONFIRMED"
    assert client.get("/bookings", headers=alice).json()["total"] == 1


def test_different_event_same_status_is_ignored(client, alice, booking, db):
    p = pending_payment(client, alice, booking)
    webhook(client, evt(p["provider_ref"], event_id="a"))
    r = webhook(client, evt(p["provider_ref"], event_id="b"))
    assert r.json()["result"] == "ignored"
    assert count(db, WebhookEvent) == 2


def test_late_conflicting_webhook_cannot_corrupt_settled_payment(client, alice, booking):
    p = client.post("/payments/", headers=alice, json={"booking_id": booking["id"],
                                                        "simulate_outcome": "SUCCESS"}).json()
    r = webhook(client, evt(p["provider_ref"], "FAILED", "late"))
    assert r.json()["result"] == "ignored" and r.json()["payment_status"] == "SUCCESS"
    assert booking_status(client, alice, booking) == "CONFIRMED"


def test_success_for_cancelled_booking_keeps_it_cancelled(client, alice, booking):
    p = pending_payment(client, alice, booking)
    client.post(f"/bookings/{booking['id']}/cancel", headers=alice)
    r = webhook(client, evt(p["provider_ref"]))
    assert r.json()["payment_status"] == "SUCCESS" and r.json()["booking_status"] == "CANCELLED"


def test_invalid_payloads(client):
    assert webhook(client, {"event_id": "x"}).status_code == 422
    assert webhook(client, {"event_id": "x", "payment_ref": "p", "status": "MAYBE"}).status_code == 422
    assert webhook(client, {"event_id": "", "payment_ref": "p", "status": "SUCCESS"}).status_code == 422
    body = client.post("/payments/webhook/", content=b"not json",
                       headers={"X-Webhook-Signature": __import__("app.services.webhooks", fromlist=["sign"]).sign(b"not json")})
    assert body.status_code == 422


def test_unknown_payment_is_recorded_and_retried_later(client, alice, booking, db):
    r = webhook(client, evt("pay_doesnotexist"))
    assert r.status_code == 404 and r.json()["code"] == "payment_not_found"
    ev = db.scalar(select(WebhookEvent))
    assert ev.status == WebhookEventStatus.FAILED and ev.attempts == 1

    # The payment shows up later (e.g. webhook raced our commit)
    p = pending_payment(client, alice, booking)
    db.expire_all()
    ev = db.scalar(select(WebhookEvent))
    ev.payload = {**ev.payload, "payment_ref": p["provider_ref"]}
    ev.payment_ref = p["provider_ref"]
    db.commit()

    from datetime import datetime, timedelta, timezone
    assert retry_failed_events(db, now=datetime.now(timezone.utc)) == 0  # still backing off
    assert retry_failed_events(db, now=datetime.now(timezone.utc) + timedelta(minutes=5)) == 1
    db.expire_all()
    assert db.scalar(select(WebhookEvent)).status == WebhookEventStatus.PROCESSED
    assert booking_status(client, alice, booking) == "CONFIRMED"


def test_provider_redelivery_of_failed_event_reprocesses(client, alice, booking, db):
    # First delivery fails (unknown ref) ...
    assert webhook(client, evt("pay_late", event_id="e9")).status_code == 404
    # ... payment gets created with that ref, provider redelivers the same event_id
    p = pending_payment(client, alice, booking)
    db_pay = db.scalar(select(Payment))
    db_pay.provider_ref = "pay_late"
    db.commit()
    r = webhook(client, evt("pay_late", event_id="e9"))
    assert r.status_code == 200 and r.json()["result"] == "processed"
    assert booking_status(client, alice, booking) == "CONFIRMED"


def test_retries_stop_after_max_attempts(client, db):
    from datetime import datetime, timedelta, timezone
    from app.core.config import get_settings
    webhook(client, evt("pay_nope"))
    ev = db.scalar(select(WebhookEvent))
    ev.attempts = get_settings().webhook_max_attempts
    db.commit()
    assert retry_failed_events(db, now=datetime.now(timezone.utc) + timedelta(days=1)) == 0
