def test_create_loan(client, auth_headers):
    resp = client.post(
        "/api/loans/",
        json={
            "name": "Car Loan",
            "bank": "Bank of America",
            "principal_amount": 20000.0,
            "remaining_balance": 18000.0,
            "interest_rate": 5.5,
            "monthly_payment": 380.0,
            "currency": "USD",
            "start_date": "2023-01-01",
            "end_date": "2027-01-01",
        },
        headers=auth_headers,
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["name"] == "Car Loan"
    assert data["remaining_balance"] == 18000.0


def test_list_loans(client, auth_headers):
    resp = client.get("/api/loans/", headers=auth_headers)
    assert resp.status_code == 200
    assert len(resp.json()) >= 1


def test_loan_payment_reduces_balance(client, auth_headers):
    loan_resp = client.post(
        "/api/loans/",
        json={
            "name": "Personal Loan",
            "principal_amount": 5000.0,
            "remaining_balance": 5000.0,
            "interest_rate": 8.0,
            "monthly_payment": 200.0,
            "currency": "USD",
        },
        headers=auth_headers,
    )
    loan_id = loan_resp.json()["id"]
    payment_resp = client.post(
        f"/api/loans/{loan_id}/payments",
        json={"amount": 200.0, "principal_portion": 170.0, "interest_portion": 30.0, "payment_date": "2024-02-01"},
        headers=auth_headers,
    )
    assert payment_resp.status_code == 201
    loan_after = client.get(f"/api/loans/{loan_id}", headers=auth_headers).json()
    assert loan_after["remaining_balance"] == 5000.0 - 170.0


def test_delete_payment_restores_balance(client, auth_headers):
    loan_resp = client.post(
        "/api/loans/",
        json={
            "name": "Del Loan",
            "principal_amount": 3000.0,
            "remaining_balance": 3000.0,
            "interest_rate": 6.0,
            "monthly_payment": 100.0,
            "currency": "USD",
        },
        headers=auth_headers,
    )
    loan_id = loan_resp.json()["id"]
    pay_resp = client.post(
        f"/api/loans/{loan_id}/payments",
        json={"amount": 100.0, "principal_portion": 80.0, "payment_date": "2024-03-01"},
        headers=auth_headers,
    )
    pay_id = pay_resp.json()["id"]
    client.delete(f"/api/loans/{loan_id}/payments/{pay_id}", headers=auth_headers)
    loan_after = client.get(f"/api/loans/{loan_id}", headers=auth_headers).json()
    assert loan_after["remaining_balance"] == 3000.0


def test_delete_loan(client, auth_headers):
    loan_resp = client.post(
        "/api/loans/",
        json={"name": "To Delete", "principal_amount": 1000.0, "remaining_balance": 1000.0,
              "interest_rate": 3.0, "monthly_payment": 50.0, "currency": "USD"},
        headers=auth_headers,
    )
    loan_id = loan_resp.json()["id"]
    resp = client.delete(f"/api/loans/{loan_id}", headers=auth_headers)
    assert resp.status_code == 204
