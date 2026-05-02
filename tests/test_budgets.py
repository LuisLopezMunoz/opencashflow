from datetime import date

import pytest


@pytest.fixture(scope="module")
def budget_id(client, auth_headers):
    resp = client.post(
        "/api/budgets/",
        json={
            "name": "Monthly Budget",
            "period_type": "monthly",
            "start_date": str(date.today()),
            "total_amount": 3000.0,
        },
        headers=auth_headers,
    )
    assert resp.status_code == 201
    return resp.json()["id"]


def test_create_budget(client, auth_headers):
    resp = client.post(
        "/api/budgets/",
        json={
            "name": "Vacation 2026",
            "period_type": "custom",
            "start_date": "2026-06-01",
            "end_date": "2026-08-31",
            "total_amount": 5000.0,
            "currency": "USD",
        },
        headers=auth_headers,
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["name"] == "Vacation 2026"
    assert data["total_amount"] == 5000.0


def test_list_budgets(client, auth_headers):
    resp = client.get("/api/budgets/", headers=auth_headers)
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)
    assert len(resp.json()) >= 1


def test_get_budget_with_categories(client, auth_headers, budget_id):
    resp = client.get(f"/api/budgets/{budget_id}", headers=auth_headers)
    assert resp.status_code == 200
    data = resp.json()
    assert data["id"] == budget_id
    assert "categories" in data


def test_update_budget(client, auth_headers, budget_id):
    resp = client.put(
        f"/api/budgets/{budget_id}",
        json={"total_amount": 3500.0},
        headers=auth_headers,
    )
    assert resp.status_code == 200
    assert resp.json()["total_amount"] == 3500.0


def test_create_budget_category(client, auth_headers, budget_id):
    resp = client.post(
        f"/api/budgets/{budget_id}/categories",
        json={"name": "Food", "allocated_amount": 500.0},
        headers=auth_headers,
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["name"] == "Food"
    assert data["allocated_amount"] == 500.0


def test_list_budget_categories(client, auth_headers, budget_id):
    resp = client.get(f"/api/budgets/{budget_id}/categories", headers=auth_headers)
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)
    assert len(resp.json()) >= 1


def test_update_budget_category(client, auth_headers, budget_id):
    # Create a category to update
    resp = client.post(
        f"/api/budgets/{budget_id}/categories",
        json={"name": "Transport", "allocated_amount": 200.0},
        headers=auth_headers,
    )
    cat_id = resp.json()["id"]
    resp = client.put(
        f"/api/budgets/{budget_id}/categories/{cat_id}",
        json={"allocated_amount": 250.0},
        headers=auth_headers,
    )
    assert resp.status_code == 200
    assert resp.json()["allocated_amount"] == 250.0


def test_delete_budget_category(client, auth_headers, budget_id):
    resp = client.post(
        f"/api/budgets/{budget_id}/categories",
        json={"name": "To delete", "allocated_amount": 0.0},
        headers=auth_headers,
    )
    cat_id = resp.json()["id"]
    resp = client.delete(
        f"/api/budgets/{budget_id}/categories/{cat_id}", headers=auth_headers
    )
    assert resp.status_code == 204


def test_delete_budget(client, auth_headers):
    resp = client.post(
        "/api/budgets/",
        json={
            "name": "To delete",
            "period_type": "monthly",
            "start_date": str(date.today()),
        },
        headers=auth_headers,
    )
    bud_id = resp.json()["id"]
    resp = client.delete(f"/api/budgets/{bud_id}", headers=auth_headers)
    assert resp.status_code == 204
    resp = client.get(f"/api/budgets/{bud_id}", headers=auth_headers)
    assert resp.status_code == 404


def test_budget_not_found(client, auth_headers):
    resp = client.get("/api/budgets/99999", headers=auth_headers)
    assert resp.status_code == 404
