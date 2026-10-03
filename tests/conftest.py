import os

# Must be set before the app is imported.
os.environ.update(
    DATABASE_URL=os.environ.get("TEST_DATABASE_URL", "sqlite://"),
    REDIS_URL="",
    AUTO_CREATE_TABLES="false",
    BCRYPT_ROUNDS="4",
    ADMIN_EMAILS="admin@example.com",
    WEBHOOK_SECRET="test-webhook-secret",
    RATE_LIMIT_ENABLED="false",
    ENVIRONMENT="test",
    JWT_SECRET="test-secret-that-is-at-least-32-bytes-long",
)

import json  # noqa: E402
from datetime import datetime, timedelta, timezone  # noqa: E402

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.core.cache import reset_cache  # noqa: E402
from app.db.session import SessionLocal, engine  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Base  # noqa: E402
from app.services.webhooks import sign  # noqa: E402

PASSWORD = "Passw0rd123"


@pytest.fixture(autouse=True)
def _fresh_state():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    reset_cache()
    yield


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def db():
    with SessionLocal() as session:
        yield session


def signup_and_login(client, email, name="Test User"):
    r = client.post("/auth/signup", json={"email": email, "password": PASSWORD, "full_name": name})
    assert r.status_code == 201, r.text
    r = client.post("/auth/login", json={"email": email, "password": PASSWORD})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


@pytest.fixture
def admin(client):
    return signup_and_login(client, "admin@example.com", "Admin")


@pytest.fixture
def alice(client):
    return signup_and_login(client, "alice@example.com", "Alice")


@pytest.fixture
def bob(client):
    return signup_and_login(client, "bob@example.com", "Bob")


@pytest.fixture
def catalog(client, admin):
    """One centre offering one test at 1200.00."""
    t = client.post("/tests", headers=admin,
                    json={"code": "cbc", "name": "Complete Blood Count"}).json()
    c = client.post("/centres", headers=admin,
                    json={"name": "EVE Central", "address": "1 MG Road", "city": "Bengaluru"}).json()
    r = client.put(f"/centres/{c['id']}/tests/{t['id']}", headers=admin, json={"price": "1200.00"})
    assert r.status_code == 200, r.text
    return {"test": t, "centre": c}


def future(days=2):
    return (datetime.now(timezone.utc) + timedelta(days=days)).replace(microsecond=0).isoformat()


@pytest.fixture
def booking(client, alice, catalog):
    r = client.post("/bookings", headers=alice, json={
        "centre_id": catalog["centre"]["id"], "test_id": catalog["test"]["id"],
        "appointment_at": future()})
    assert r.status_code == 201, r.text
    return r.json()


def webhook(client, payload: dict, signature: str | None = "auto"):
    body = json.dumps(payload).encode()
    headers = {"Content-Type": "application/json"}
    if signature == "auto":
        headers["X-Webhook-Signature"] = sign(body)
    elif signature:
        headers["X-Webhook-Signature"] = signature
    return client.post("/payments/webhook/", content=body, headers=headers)
