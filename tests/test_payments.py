def pay(client, headers, booking_id, outcome=None, **kw):
    body = {"booking_id": booking_id}
    if outcome:
        body["simulate_outcome"] = outcome
    return client.post("/payments/", headers=headers, json=body, **kw)


def status_of(client, headers, booking_id):
    return client.get(f"/bookings/{booking_id}", headers=headers).json()["status"]


def test_successful_payment_confirms_booking(client, alice, booking):
    r = pay(client, alice, booking["id"], "SUCCESS")
    assert r.status_code == 201
    p = r.json()
    assert p["status"] == "SUCCESS" and p["amount"] == "1200.00" and p["provider_ref"].startswith("pay_")
    assert status_of(client, alice, booking["id"]) == "CONFIRMED"


def test_failed_payment_marks_booking_failed_then_retry_succeeds(client, alice, booking):
    p1 = pay(client, alice, booking["id"], "FAILED").json()
    assert p1["status"] == "FAILED" and p1["failure_reason"]
    assert status_of(client, alice, booking["id"]) == "FAILED"
    p2 = pay(client, alice, booking["id"], "SUCCESS")
    assert p2.status_code == 201 and p2.json()["id"] != p1["id"]
    assert status_of(client, alice, booking["id"]) == "CONFIRMED"
    assert client.get(f"/payments?booking_id={booking['id']}", headers=alice).json()["total"] == 2


def test_random_outcome_is_success_or_failed(client, alice, booking):
    assert pay(client, alice, booking["id"]).json()["status"] in ("SUCCESS", "FAILED")


def test_cannot_pay_twice_or_for_cancelled(client, alice, booking):
    pay(client, alice, booking["id"], "SUCCESS")
    r = pay(client, alice, booking["id"], "SUCCESS")
    assert r.status_code == 409 and r.json()["code"] == "already_paid"


def test_cannot_pay_cancelled_booking(client, alice, booking):
    client.post(f"/bookings/{booking['id']}/cancel", headers=alice)
    r = pay(client, alice, booking["id"], "SUCCESS")
    assert r.status_code == 409 and r.json()["code"] == "booking_cancelled"


def test_authorization_and_invalid_ids(client, alice, bob, booking):
    assert client.post("/payments/", json={"booking_id": booking["id"]}).status_code == 401
    assert pay(client, bob, booking["id"]).status_code == 404  # someone else's booking
    assert pay(client, alice, 999999).status_code == 404
    assert client.post("/payments/", headers=alice, json={"booking_id": "x"}).status_code == 422
    assert client.post("/payments/", headers=alice, json={"booking_id": booking["id"],
                                                          "simulate_outcome": "MAYBE"}).status_code == 422
    p = pay(client, alice, booking["id"], "SUCCESS").json()
    assert client.get(f"/payments/{p['id']}", headers=alice).status_code == 200
    assert client.get(f"/payments/{p['id']}", headers=bob).status_code == 404


def test_client_cannot_choose_amount(client, alice, booking):
    r = client.post("/payments/", headers=alice, json={"booking_id": booking["id"], "amount": "1",
                                                       "simulate_outcome": "SUCCESS"})
    assert r.json()["amount"] == "1200.00"


def test_idempotency_key_replays_same_payment(client, alice, booking):
    h = {**alice, "Idempotency-Key": "abc-123"}
    first = pay(client, h, booking["id"], "SUCCESS")
    second = pay(client, h, booking["id"], "SUCCESS")
    assert first.status_code == 201 and second.status_code == 200
    assert first.json()["id"] == second.json()["id"]
    assert client.get("/payments", headers=alice).json()["total"] == 1


def test_idempotency_key_reuse_for_other_booking_conflicts(client, alice, catalog, booking):
    from tests.conftest import future
    other = client.post("/bookings", headers=alice, json={
        "centre_id": catalog["centre"]["id"], "test_id": catalog["test"]["id"],
        "appointment_at": future(5)}).json()
    h = {**alice, "Idempotency-Key": "k1"}
    assert pay(client, h, booking["id"], "SUCCESS").status_code == 201
    r = pay(client, h, other["id"], "SUCCESS")
    assert r.status_code == 409 and r.json()["code"] == "idempotency_key_reused"


def test_pending_payment_blocks_a_second_one(client, alice, booking):
    assert pay(client, alice, booking["id"], "PENDING").json()["status"] == "PENDING"
    assert status_of(client, alice, booking["id"]) == "PENDING"
    r = pay(client, alice, booking["id"], "SUCCESS")
    assert r.status_code == 409 and r.json()["code"] == "payment_in_progress"


def test_payment_rate_limit(client, alice, booking, monkeypatch):
    from app.core.config import get_settings
    monkeypatch.setattr(get_settings(), "rate_limit_enabled", True)
    monkeypatch.setattr(get_settings(), "payment_rate_limit_per_minute", 2)
    codes = [pay(client, alice, booking["id"], "FAILED").status_code for _ in range(4)]
    assert codes == [201, 201, 429, 429]


def test_simulate_outcome_ignored_when_disabled(client, alice, booking, monkeypatch):
    from app.core.config import get_settings
    monkeypatch.setattr(get_settings(), "allow_simulated_outcome", False)
    monkeypatch.setattr(get_settings(), "payment_success_rate", 1.0)
    assert pay(client, alice, booking["id"], "FAILED").json()["status"] == "SUCCESS"
