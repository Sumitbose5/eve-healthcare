from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.models import Booking, BookingStatus
from app.services.bookings import expire_stale_pending


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200 and r.json() == {"status": "ok", "database": "ok", "cache": "memory"}


def test_request_id_header(client):
    r = client.get("/health", headers={"X-Request-ID": "abc123"})
    assert r.headers["X-Request-ID"] == "abc123"
    assert "X-Request-ID" in client.get("/health").headers


def test_expire_stale_pending_bookings(client, alice, booking, db):
    later = datetime.now(timezone.utc) + timedelta(hours=1)
    assert expire_stale_pending(db, 30, now=datetime.now(timezone.utc)) == 0   # too fresh
    assert expire_stale_pending(db, 30, now=later) == 1
    db.expire_all()
    assert db.scalar(select(Booking)).status == BookingStatus.CANCELLED


def test_expiry_skips_bookings_with_payment_in_flight(client, alice, booking, db):
    client.post("/payments/", headers=alice, json={"booking_id": booking["id"],
                                                    "simulate_outcome": "PENDING"})
    later = datetime.now(timezone.utc) + timedelta(hours=1)
    assert expire_stale_pending(db, 30, now=later) == 0


def test_expiry_leaves_confirmed_bookings_alone(client, alice, booking, db):
    client.post("/payments/", headers=alice, json={"booking_id": booking["id"],
                                                    "simulate_outcome": "SUCCESS"})
    later = datetime.now(timezone.utc) + timedelta(hours=1)
    assert expire_stale_pending(db, 30, now=later) == 0
