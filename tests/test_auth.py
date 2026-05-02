def test_register_and_login(client):
    resp = client.post(
        "/api/auth/register",
        json={"username": "newuser", "email": "new@example.com", "password": "secret"},
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["username"] == "newuser"
    assert data["email"] == "new@example.com"
    assert "id" in data


def test_register_duplicate_username(client):
    client.post(
        "/api/auth/register",
        json={"username": "dupuser", "email": "dup1@example.com", "password": "secret"},
    )
    resp = client.post(
        "/api/auth/register",
        json={"username": "dupuser", "email": "dup2@example.com", "password": "secret"},
    )
    assert resp.status_code == 400


def test_login(client, auth_headers):
    # auth_headers fixture ensures "testuser" exists before this test runs
    resp = client.post(
        "/api/auth/token",
        data={"username": "testuser", "password": "pass1234"},
    )
    assert resp.status_code == 200
    assert "access_token" in resp.json()


def test_login_wrong_password(client):
    resp = client.post(
        "/api/auth/token",
        data={"username": "testuser", "password": "wrongpassword"},
    )
    assert resp.status_code == 401


def test_get_me(client, auth_headers):
    resp = client.get("/api/auth/me", headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json()["username"] == "testuser"


def test_protected_without_token(client):
    resp = client.get("/api/wallets/")
    assert resp.status_code == 401
