def test_create_credit_card(client, auth_headers):
    resp = client.post(
        "/api/credit-cards/",
        json={
            "name": "Visa Rewards",
            "bank": "Chase",
            "credit_limit": 5000.0,
            "current_balance": 250.0,
            "currency": "USD",
            "closing_day": 25,
            "due_day": 15,
        },
        headers=auth_headers,
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["name"] == "Visa Rewards"
    assert data["credit_limit"] == 5000.0


def test_list_credit_cards(client, auth_headers):
    resp = client.get("/api/credit-cards/", headers=auth_headers)
    assert resp.status_code == 200
    assert len(resp.json()) >= 1


def test_create_charge_updates_balance(client, auth_headers):
    card_resp = client.post(
        "/api/credit-cards/",
        json={"name": "Test Card", "credit_limit": 1000.0, "current_balance": 0.0, "currency": "USD"},
        headers=auth_headers,
    )
    card_id = card_resp.json()["id"]
    charge_resp = client.post(
        f"/api/credit-cards/{card_id}/charges",
        json={"amount": 150.0, "description": "Groceries", "category": "Food", "charge_date": "2024-01-10"},
        headers=auth_headers,
    )
    assert charge_resp.status_code == 201
    card_after = client.get(f"/api/credit-cards/{card_id}", headers=auth_headers).json()
    assert card_after["current_balance"] == 150.0


def test_delete_charge_updates_balance(client, auth_headers):
    card_resp = client.post(
        "/api/credit-cards/",
        json={"name": "Del Card", "credit_limit": 2000.0, "current_balance": 0.0, "currency": "USD"},
        headers=auth_headers,
    )
    card_id = card_resp.json()["id"]
    charge_resp = client.post(
        f"/api/credit-cards/{card_id}/charges",
        json={"amount": 300.0, "charge_date": "2024-01-11"},
        headers=auth_headers,
    )
    charge_id = charge_resp.json()["id"]
    client.delete(f"/api/credit-cards/{card_id}/charges/{charge_id}", headers=auth_headers)
    card_after = client.get(f"/api/credit-cards/{card_id}", headers=auth_headers).json()
    assert card_after["current_balance"] == 0.0


def test_delete_credit_card(client, auth_headers):
    card_resp = client.post(
        "/api/credit-cards/",
        json={"name": "ToDelete", "credit_limit": 500.0, "current_balance": 0.0, "currency": "USD"},
        headers=auth_headers,
    )
    card_id = card_resp.json()["id"]
    resp = client.delete(f"/api/credit-cards/{card_id}", headers=auth_headers)
    assert resp.status_code == 204
