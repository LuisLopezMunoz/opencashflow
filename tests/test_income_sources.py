def test_create_income_source(client, auth_headers):
    resp = client.post(
        "/api/income-sources/",
        json={
            "name": "Day Job",
            "income_type": "salary",
            "amount": 3500.0,
            "frequency": "monthly",
            "currency": "USD",
            "is_active": True,
            "description": "Primary income",
        },
        headers=auth_headers,
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["name"] == "Day Job"
    assert data["amount"] == 3500.0


def test_list_income_sources(client, auth_headers):
    resp = client.get("/api/income-sources/", headers=auth_headers)
    assert resp.status_code == 200
    assert len(resp.json()) >= 1


def test_update_income_source(client, auth_headers):
    create_resp = client.post(
        "/api/income-sources/",
        json={"name": "Freelance", "income_type": "freelance", "amount": 800.0, "frequency": "monthly", "currency": "USD"},
        headers=auth_headers,
    )
    src_id = create_resp.json()["id"]
    resp = client.put(
        f"/api/income-sources/{src_id}",
        json={"amount": 1200.0, "is_active": False},
        headers=auth_headers,
    )
    assert resp.status_code == 200
    assert resp.json()["amount"] == 1200.0
    assert resp.json()["is_active"] is False


def test_delete_income_source(client, auth_headers):
    create_resp = client.post(
        "/api/income-sources/",
        json={"name": "ToDelete", "income_type": "other", "amount": 100.0, "frequency": "monthly", "currency": "USD"},
        headers=auth_headers,
    )
    src_id = create_resp.json()["id"]
    resp = client.delete(f"/api/income-sources/{src_id}", headers=auth_headers)
    assert resp.status_code == 204


def test_income_source_not_found(client, auth_headers):
    resp = client.get("/api/income-sources/99999", headers=auth_headers)
    assert resp.status_code == 404
