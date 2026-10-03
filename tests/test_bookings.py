from tests.conftest import future


def payload(catalog, **kw):
    return {"centre_id": catalog["centre"]["id"], "test_id": catalog["test"]["id"],
            "appointment_at": future(), **kw}


def test_create_booking(client, alice, catalog):
    r = client.post("/bookings", headers=alice, json=payload(catalog))
    assert r.status_code == 201
    b = r.json()
    assert b["status"] == "PENDING" and b["amount"] == "1200.00"
    assert b["centre_name"] == "EVE Central" and b["test_name"] == "Complete Blood Count"


def test_amount_is_price_snapshot(client, admin, alice, catalog):
    b = client.post("/bookings", headers=alice, json=payload(catalog)).json()
    client.put(f"/centres/{catalog['centre']['id']}/tests/{catalog['test']['id']}",
               headers=admin, json={"price": "2000"})
    assert client.get(f"/bookings/{b['id']}", headers=alice).json()["amount"] == "1200.00"


def test_requires_auth(client, catalog):
    assert client.post("/bookings", json=payload(catalog)).status_code == 401
    assert client.get("/bookings").status_code == 401


def test_invalid_requests(client, alice, catalog):
    assert client.post("/bookings", headers=alice,
                       json=payload(catalog, centre_id=999)).status_code == 404
    assert client.post("/bookings", headers=alice,
                       json=payload(catalog, test_id=999)).status_code == 404
    assert client.post("/bookings", headers=alice,
                       json=payload(catalog, appointment_at="2020-01-01T10:00:00+00:00")).status_code == 422
    assert client.post("/bookings", headers=alice,
                       json=payload(catalog, appointment_at="2099-01-01T10:00:00+00:00")).status_code == 422
    assert client.post("/bookings", headers=alice,
                       json=payload(catalog, appointment_at="2030-01-01T10:00:00")).status_code == 422  # naive
    assert client.post("/bookings", headers=alice,
                       json=payload(catalog, centre_id="x")).status_code == 422
    assert client.post("/bookings", headers=alice, json={}).status_code == 422


def test_test_not_offered_by_centre(client, admin, alice, catalog):
    other = client.post("/tests", headers=admin, json={"code": "lft", "name": "Liver Function"}).json()
    r = client.post("/bookings", headers=alice, json=payload(catalog, test_id=other["id"]))
    assert r.status_code == 404 and r.json()["code"] == "test_not_offered"


def test_duplicate_live_booking_rejected_but_allowed_after_cancel(client, alice, catalog):
    p = payload(catalog)
    b = client.post("/bookings", headers=alice, json=p).json()
    assert client.post("/bookings", headers=alice, json=p).status_code == 409
    client.post(f"/bookings/{b['id']}/cancel", headers=alice)
    assert client.post("/bookings", headers=alice, json=p).status_code == 201


def test_ownership_isolation(client, alice, bob, booking):
    assert client.get(f"/bookings/{booking['id']}", headers=alice).status_code == 200
    assert client.get(f"/bookings/{booking['id']}", headers=bob).status_code == 404
    assert client.post(f"/bookings/{booking['id']}/cancel", headers=bob).status_code == 404
    assert client.get("/bookings", headers=bob).json()["total"] == 0
    assert client.get("/bookings", headers=alice).json()["total"] == 1


def test_admin_can_view_any_booking_but_not_pay(client, admin, booking):
    assert client.get(f"/bookings/{booking['id']}", headers=admin).status_code == 200
    r = client.post("/payments/", headers=admin, json={"booking_id": booking["id"]})
    assert r.status_code == 404


def test_invalid_booking_ids(client, alice):
    assert client.get("/bookings/999999", headers=alice).status_code == 404
    assert client.get("/bookings/abc", headers=alice).status_code == 422
    assert client.post("/bookings/999999/cancel", headers=alice).status_code == 404


def test_cancel_flow(client, alice, booking):
    r = client.post(f"/bookings/{booking['id']}/cancel", headers=alice)
    assert r.status_code == 200 and r.json()["status"] == "CANCELLED"
    again = client.post(f"/bookings/{booking['id']}/cancel", headers=alice)
    assert again.status_code == 409 and again.json()["code"] == "already_cancelled"


def test_list_status_filter_and_pagination(client, alice, catalog):
    for d in (2, 3, 4):
        client.post("/bookings", headers=alice, json=payload(catalog, appointment_at=future(d)))
    ids = [b["id"] for b in client.get("/bookings", headers=alice).json()["items"]]
    assert ids == sorted(ids, reverse=True)
    client.post(f"/bookings/{ids[0]}/cancel", headers=alice)
    assert client.get("/bookings?status=CANCELLED", headers=alice).json()["total"] == 1
    assert client.get("/bookings?status=PENDING&limit=1", headers=alice).json()["items"].__len__() == 1
    assert client.get("/bookings?status=BOGUS", headers=alice).status_code == 422
