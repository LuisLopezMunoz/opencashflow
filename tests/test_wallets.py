def test_create_wallet(client, auth_headers):
    resp = client.post(
        "/api/wallets/",
        json={"name": "My Cash", "wallet_type": "cash", "currency": "USD", "balance": 500.0},
        headers=auth_headers,
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["name"] == "My Cash"
    assert data["balance"] == 500.0


def test_list_wallets(client, auth_headers):
    resp = client.get("/api/wallets/", headers=auth_headers)
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)
    assert len(resp.json()) >= 1


def test_get_wallet(client, auth_headers):
    # Create first
    create_resp = client.post(
        "/api/wallets/",
        json={"name": "Bank Account", "wallet_type": "bank", "currency": "EUR", "balance": 1000.0},
        headers=auth_headers,
    )
    wallet_id = create_resp.json()["id"]
    resp = client.get(f"/api/wallets/{wallet_id}", headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json()["name"] == "Bank Account"


def test_update_wallet(client, auth_headers):
    create_resp = client.post(
        "/api/wallets/",
        json={"name": "Old Name", "wallet_type": "cash", "currency": "USD", "balance": 100.0},
        headers=auth_headers,
    )
    wallet_id = create_resp.json()["id"]
    resp = client.put(
        f"/api/wallets/{wallet_id}",
        json={"name": "New Name", "balance": 200.0},
        headers=auth_headers,
    )
    assert resp.status_code == 200
    assert resp.json()["name"] == "New Name"
    assert resp.json()["balance"] == 200.0


def test_delete_wallet(client, auth_headers):
    create_resp = client.post(
        "/api/wallets/",
        json={"name": "To Delete", "wallet_type": "cash", "currency": "USD", "balance": 0.0},
        headers=auth_headers,
    )
    wallet_id = create_resp.json()["id"]
    resp = client.delete(f"/api/wallets/{wallet_id}", headers=auth_headers)
    assert resp.status_code == 204
    get_resp = client.get(f"/api/wallets/{wallet_id}", headers=auth_headers)
    assert get_resp.status_code == 404


def test_wallet_not_found(client, auth_headers):
    resp = client.get("/api/wallets/99999", headers=auth_headers)
    assert resp.status_code == 404
