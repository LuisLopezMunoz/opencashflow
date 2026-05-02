"""Tests for cash flow projection and credit card payment projection."""


def test_cash_flow_projection(client, auth_headers):
    resp = client.get("/api/cash-flow/projection?months=3", headers=auth_headers)
    assert resp.status_code == 200
    data = resp.json()
    assert "months" in data
    assert len(data["months"]) == 3
    assert "total_net" in data
    for month in data["months"]:
        assert "month" in month
        assert "income" in month
        assert "expenses" in month
        assert "loan_payments" in month
        assert "credit_card_payments" in month
        assert "net" in month


def test_cash_flow_projection_default(client, auth_headers):
    resp = client.get("/api/cash-flow/projection", headers=auth_headers)
    assert resp.status_code == 200
    assert len(resp.json()["months"]) == 12


def test_credit_card_projection(client, auth_headers):
    # Create a credit card with an interest rate
    resp = client.post(
        "/api/credit-cards/",
        json={
            "name": "Projection Card",
            "credit_limit": 10000.0,
            "current_balance": 5000.0,
            "interest_rate": 0.24,
            "minimum_payment_rate": 0.05,
        },
        headers=auth_headers,
    )
    assert resp.status_code == 201
    card_id = resp.json()["id"]

    # Verify new fields are returned
    assert resp.json()["interest_rate"] == 0.24
    assert resp.json()["minimum_payment_rate"] == 0.05

    # Get projection
    resp = client.get(
        f"/api/credit-cards/{card_id}/projection?months=6", headers=auth_headers
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["card_id"] == card_id
    assert len(data["months"]) == 6
    for month in data["months"]:
        assert "month" in month
        assert "opening_balance" in month
        assert "interest" in month
        assert "minimum_payment" in month
        assert "closing_balance" in month


def test_transaction_with_recurrence(client, auth_headers):
    # Create a wallet first
    resp = client.post(
        "/api/wallets/",
        json={"name": "Recurrence Wallet", "wallet_type": "bank", "currency": "USD"},
        headers=auth_headers,
    )
    wallet_id = resp.json()["id"]

    # Create a periodic income transaction
    resp = client.post(
        "/api/transactions/",
        json={
            "wallet_id": wallet_id,
            "amount": 100.0,
            "transaction_type": "income",
            "recurrence": "periodic",
            "period_type": "monthly",
        },
        headers=auth_headers,
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["recurrence"] == "periodic"
    assert data["period_type"] == "monthly"

    # A periodic transaction shows up in the cash flow projection
    resp = client.get("/api/cash-flow/projection?months=1", headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json()["months"][0]["income"] >= 100.0
