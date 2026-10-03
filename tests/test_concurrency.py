"""Race-condition tests. Need row-level locking, so they only run against PostgreSQL:
    TEST_DATABASE_URL=postgresql://... pytest tests/test_concurrency.py
"""
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.db.session import engine
from app.main import app
from app.models import Payment, WebhookEvent
from tests.conftest import webhook

pytestmark = pytest.mark.skipif(engine.dialect.name != "postgresql",
                                reason="requires PostgreSQL row locking")


def test_parallel_duplicate_webhooks_apply_once(client, alice, booking, db):
    p = client.post("/payments/", headers=alice, json={
        "booking_id": booking["id"], "simulate_outcome": "PENDING"}).json()
    payload = {"event_id": "race-1", "payment_ref": p["provider_ref"], "status": "SUCCESS"}

    def deliver(_):
        return webhook(TestClient(app), payload).json()["result"]

    with ThreadPoolExecutor(8) as pool:
        results = list(pool.map(deliver, range(8)))
    assert results.count("processed") == 1
    assert results.count("duplicate") == 7
    assert db.scalar(select(func.count()).select_from(WebhookEvent)) == 1
    assert client.get(f"/bookings/{booking['id']}", headers=alice).json()["status"] == "CONFIRMED"


def test_parallel_payments_create_single_payment(client, alice, booking, db):
    def pay(_):
        return TestClient(app).post("/payments/", headers=alice, json={
            "booking_id": booking["id"], "simulate_outcome": "SUCCESS"}).status_code

    with ThreadPoolExecutor(8) as pool:
        codes = list(pool.map(pay, range(8)))
    assert codes.count(201) == 1
    assert set(codes) <= {201, 409}
    assert db.scalar(select(func.count()).select_from(Payment)) == 1
