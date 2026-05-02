import pytest


@pytest.fixture(scope="module")
def wallet_id(client, auth_headers):
    resp = client.post(
        "/api/wallets/",
        json={"name": "TX Wallet", "wallet_type": "cash", "currency": "USD", "balance": 1000.0},
        headers=auth_headers,
    )
    return resp.json()["id"]


def test_create_income_transaction(client, auth_headers, wallet_id):
    resp = client.post(
        "/api/transactions/",
        json={
            "wallet_id": wallet_id,
            "amount": 500.0,
            "transaction_type": "income",
            "category": "Salary",
            "description": "Monthly salary",
            "transaction_date": "2024-01-15",
        },
        headers=auth_headers,
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["amount"] == 500.0
    assert data["transaction_type"] == "income"
    # Check wallet balance updated
    wallet_resp = client.get(f"/api/wallets/{wallet_id}", headers=auth_headers)
    assert wallet_resp.json()["balance"] == 1500.0


def test_create_expense_transaction(client, auth_headers, wallet_id):
    resp = client.post(
        "/api/transactions/",
        json={
            "wallet_id": wallet_id,
            "amount": 100.0,
            "transaction_type": "expense",
            "category": "Food",
            "transaction_date": "2024-01-16",
        },
        headers=auth_headers,
    )
    assert resp.status_code == 201
    wallet_resp = client.get(f"/api/wallets/{wallet_id}", headers=auth_headers)
    assert wallet_resp.json()["balance"] == 1400.0


def test_list_transactions(client, auth_headers):
    resp = client.get("/api/transactions/", headers=auth_headers)
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)
    assert len(resp.json()) >= 2


def test_filter_transactions_by_type(client, auth_headers):
    resp = client.get("/api/transactions/?transaction_type=income", headers=auth_headers)
    assert resp.status_code == 200
    for tx in resp.json():
        assert tx["transaction_type"] == "income"


def test_delete_transaction_reverts_balance(client, auth_headers, wallet_id):
    create_resp = client.post(
        "/api/transactions/",
        json={
            "wallet_id": wallet_id,
            "amount": 200.0,
            "transaction_type": "income",
            "transaction_date": "2024-01-17",
        },
        headers=auth_headers,
    )
    tx_id = create_resp.json()["id"]
    wallet_before = client.get(f"/api/wallets/{wallet_id}", headers=auth_headers).json()["balance"]
    client.delete(f"/api/transactions/{tx_id}", headers=auth_headers)
    wallet_after = client.get(f"/api/wallets/{wallet_id}", headers=auth_headers).json()["balance"]
    assert abs(wallet_after - (wallet_before - 200.0)) < 0.01
