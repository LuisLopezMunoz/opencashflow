import pytest


@pytest.fixture(scope="module")
def wishlist_id(client, auth_headers):
    resp = client.post(
        "/api/wishlists/",
        json={"name": "My Wish List", "description": "Test list"},
        headers=auth_headers,
    )
    assert resp.status_code == 201
    return resp.json()["id"]


def test_create_wishlist(client, auth_headers):
    resp = client.post(
        "/api/wishlists/",
        json={"name": "Holiday Gifts"},
        headers=auth_headers,
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["name"] == "Holiday Gifts"
    assert "id" in data


def test_list_wishlists(client, auth_headers):
    resp = client.get("/api/wishlists/", headers=auth_headers)
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)
    assert len(resp.json()) >= 1


def test_get_wishlist(client, auth_headers, wishlist_id):
    resp = client.get(f"/api/wishlists/{wishlist_id}", headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json()["id"] == wishlist_id


def test_update_wishlist(client, auth_headers, wishlist_id):
    resp = client.put(
        f"/api/wishlists/{wishlist_id}",
        json={"name": "Updated Wish List"},
        headers=auth_headers,
    )
    assert resp.status_code == 200
    assert resp.json()["name"] == "Updated Wish List"


def test_create_wishlist_item(client, auth_headers, wishlist_id):
    resp = client.post(
        f"/api/wishlists/{wishlist_id}/items",
        json={
            "name": "New Laptop",
            "estimated_price": 1200.0,
            "category": "Electronics",
            "priority": 1,
        },
        headers=auth_headers,
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["name"] == "New Laptop"
    assert data["category"] == "Electronics"
    assert data["is_purchased"] is False


def test_list_wishlist_items(client, auth_headers, wishlist_id):
    resp = client.get(f"/api/wishlists/{wishlist_id}/items", headers=auth_headers)
    assert resp.status_code == 200
    items = resp.json()
    assert len(items) >= 1


def test_update_wishlist_item(client, auth_headers, wishlist_id):
    # Create an item first
    resp = client.post(
        f"/api/wishlists/{wishlist_id}/items",
        json={"name": "Headphones", "estimated_price": 200.0},
        headers=auth_headers,
    )
    item_id = resp.json()["id"]
    # Mark as purchased
    resp = client.put(
        f"/api/wishlists/{wishlist_id}/items/{item_id}",
        json={"is_purchased": True},
        headers=auth_headers,
    )
    assert resp.status_code == 200
    assert resp.json()["is_purchased"] is True


def test_delete_wishlist_item(client, auth_headers, wishlist_id):
    resp = client.post(
        f"/api/wishlists/{wishlist_id}/items",
        json={"name": "To delete"},
        headers=auth_headers,
    )
    item_id = resp.json()["id"]
    resp = client.delete(
        f"/api/wishlists/{wishlist_id}/items/{item_id}", headers=auth_headers
    )
    assert resp.status_code == 204


def test_delete_wishlist(client, auth_headers):
    resp = client.post(
        "/api/wishlists/",
        json={"name": "To delete"},
        headers=auth_headers,
    )
    wl_id = resp.json()["id"]
    resp = client.delete(f"/api/wishlists/{wl_id}", headers=auth_headers)
    assert resp.status_code == 204
    resp = client.get(f"/api/wishlists/{wl_id}", headers=auth_headers)
    assert resp.status_code == 404


def test_wishlist_not_found(client, auth_headers):
    resp = client.get("/api/wishlists/99999", headers=auth_headers)
    assert resp.status_code == 404
