def test_summary_endpoint(client, auth_headers):
    resp = client.get("/api/summary/", headers=auth_headers)
    assert resp.status_code == 200
    data = resp.json()
    assert "total_wallet_balance" in data
    assert "total_credit_card_balance" in data
    assert "total_loan_balance" in data
    assert "total_monthly_income" in data
    assert "net_monthly_cash_flow" in data
    assert "wallets_count" in data
    assert "credit_cards_count" in data
    assert "loans_count" in data
    assert "active_income_sources" in data


def test_health_check(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"
