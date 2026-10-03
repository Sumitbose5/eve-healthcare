def test_only_admin_can_write(client, alice, catalog):
    body = {"name": "X Centre", "address": "addr", "city": "Pune"}
    assert client.post("/centres", json=body).status_code == 401
    assert client.post("/centres", json=body, headers=alice).status_code == 403
    assert client.post("/tests", json={"code": "abc", "name": "ABC"}, headers=alice).status_code == 403
    cid, tid = catalog["centre"]["id"], catalog["test"]["id"]
    assert client.put(f"/centres/{cid}/tests/{tid}", json={"price": "1"}, headers=alice).status_code == 403
    assert client.delete(f"/centres/{cid}/tests/{tid}", headers=alice).status_code == 403


def test_public_read_and_shape(client, catalog):
    r = client.get("/centres")
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 1 and body["limit"] == 20
    centre = body["items"][0]
    assert centre["city"] == "Bengaluru"
    assert centre["tests"] == [{"test_id": catalog["test"]["id"], "test_code": "CBC",
                                "test_name": "Complete Blood Count", "price": "1200.00"}]
    assert client.get(f"/centres/{centre['id']}").json()["tests"][0]["price"] == "1200.00"


def test_filters_and_pagination(client, admin, catalog):
    t2 = client.post("/tests", headers=admin, json={"code": "lft", "name": "Liver Function"}).json()
    for i in range(3):
        c = client.post("/centres", headers=admin, json={
            "name": f"Centre {i}", "address": "addr", "city": "Mumbai"}).json()
        if i == 0:
            client.put(f"/centres/{c['id']}/tests/{t2['id']}", headers=admin, json={"price": "500"})
    assert client.get("/centres?city=mumbai").json()["total"] == 3
    assert client.get("/centres?city=mumbai&limit=2").json()["items"].__len__() == 2
    assert client.get("/centres?city=mumbai&limit=2&offset=2").json()["items"].__len__() == 1
    assert client.get(f"/centres?test_id={t2['id']}").json()["total"] == 1
    assert client.get("/centres?q=eve").json()["total"] == 1
    assert client.get("/centres?limit=0").status_code == 422
    assert client.get("/centres?limit=101").status_code == 422
    assert client.get("/tests?q=liver").json()["total"] == 1


def test_price_update_invalidates_cache(client, admin, catalog):
    cid, tid = catalog["centre"]["id"], catalog["test"]["id"]
    assert client.get("/centres").json()["items"][0]["tests"][0]["price"] == "1200.00"  # primes cache
    client.get(f"/centres/{cid}")
    r = client.put(f"/centres/{cid}/tests/{tid}", headers=admin, json={"price": "999.50"})
    assert r.status_code == 200
    assert client.get("/centres").json()["items"][0]["tests"][0]["price"] == "999.50"
    assert client.get(f"/centres/{cid}").json()["tests"][0]["price"] == "999.50"


def test_remove_test_from_centre(client, admin, catalog):
    cid, tid = catalog["centre"]["id"], catalog["test"]["id"]
    assert client.delete(f"/centres/{cid}/tests/{tid}", headers=admin).status_code == 204
    assert client.get(f"/centres/{cid}").json()["tests"] == []
    assert client.delete(f"/centres/{cid}/tests/{tid}", headers=admin).status_code == 404


def test_validation_and_conflicts(client, admin, catalog):
    cid, tid = catalog["centre"]["id"], catalog["test"]["id"]
    assert client.put(f"/centres/{cid}/tests/{tid}", headers=admin, json={"price": "0"}).status_code == 422
    assert client.put(f"/centres/{cid}/tests/{tid}", headers=admin, json={"price": "-5"}).status_code == 422
    assert client.put(f"/centres/{cid}/tests/{tid}", headers=admin, json={"price": "1.234"}).status_code == 422
    assert client.put(f"/centres/999/tests/{tid}", headers=admin, json={"price": "5"}).status_code == 404
    assert client.put(f"/centres/{cid}/tests/999", headers=admin, json={"price": "5"}).status_code == 404
    dup = client.post("/centres", headers=admin, json={"name": "EVE Central", "address": "x y",
                                                        "city": "Bengaluru"})
    assert dup.status_code == 409
    assert client.post("/tests", headers=admin, json={"code": "CBC", "name": "Other"}).status_code == 409
    assert client.post("/centres", headers=admin, json={"name": "N", "address": "aa", "city": "cc",
                                                         "latitude": 123}).status_code == 422
    assert client.get("/centres/999").status_code == 404
    assert client.get("/centres/abc").status_code == 422
