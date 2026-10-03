from datetime import datetime, timedelta, timezone

import jwt

from tests.conftest import PASSWORD, signup_and_login


def test_signup_login_me(client):
    h = signup_and_login(client, "Jane@Example.com", "Jane")
    me = client.get("/auth/me", headers=h).json()
    assert me["email"] == "jane@example.com" and me["role"] == "USER"
    assert "password" not in me and "password_hash" not in me


def test_admin_email_gets_admin_role(client, admin):
    assert client.get("/auth/me", headers=admin).json()["role"] == "ADMIN"


def test_duplicate_email_conflict(client):
    signup_and_login(client, "a@example.com")
    r = client.post("/auth/signup", json={"email": "A@example.com", "password": PASSWORD,
                                          "full_name": "x"})
    assert r.status_code == 409 and r.json()["code"] == "email_taken"


def test_signup_validation(client):
    for body in [
        {"email": "not-an-email", "password": PASSWORD, "full_name": "x"},
        {"email": "a@example.com", "password": "short1", "full_name": "x"},
        {"email": "a@example.com", "password": "onlyletters", "full_name": "x"},
        {"email": "a@example.com", "password": PASSWORD, "full_name": "   "},
        {"email": "a@example.com", "password": PASSWORD},
    ]:
        assert client.post("/auth/signup", json=body).status_code == 422


def test_login_wrong_password_and_unknown_user(client):
    signup_and_login(client, "a@example.com")
    r1 = client.post("/auth/login", json={"email": "a@example.com", "password": "Wrong1234"})
    r2 = client.post("/auth/login", json={"email": "ghost@example.com", "password": "Wrong1234"})
    assert r1.status_code == r2.status_code == 401
    assert r1.json() == r2.json()  # no user enumeration


def test_swagger_form_login(client):
    signup_and_login(client, "a@example.com")
    r = client.post("/auth/token", data={"username": "a@example.com", "password": PASSWORD})
    assert r.status_code == 200 and r.json()["token_type"] == "bearer"


def test_protected_routes_reject_missing_bad_and_expired_tokens(client):
    assert client.get("/auth/me").status_code == 401
    assert client.get("/auth/me", headers={"Authorization": "Bearer garbage"}).status_code == 401
    expired = jwt.encode({"sub": "1", "exp": datetime.now(timezone.utc) - timedelta(minutes=1)},
                         "test-secret-that-is-at-least-32-bytes-long", algorithm="HS256")
    assert client.get("/auth/me", headers={"Authorization": f"Bearer {expired}"}).status_code == 401
    forged = jwt.encode({"sub": "1", "exp": datetime.now(timezone.utc) + timedelta(hours=1)},
                        "wrong-secret-that-is-at-least-32-bytes-long", algorithm="HS256")
    assert client.get("/auth/me", headers={"Authorization": f"Bearer {forged}"}).status_code == 401


def test_rate_limit_on_auth(client, monkeypatch):
    from app.core.config import get_settings
    monkeypatch.setattr(get_settings(), "rate_limit_enabled", True)
    monkeypatch.setattr(get_settings(), "auth_rate_limit_per_minute", 3)
    codes = [client.post("/auth/login", json={"email": "a@example.com", "password": "x"}).status_code
             for _ in range(5)]
    assert codes[:3] == [401, 401, 401] and codes[3:] == [429, 429]
