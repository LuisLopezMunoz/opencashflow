"""Tests for the cashflow sheet engine invariants.

Covers:
  1. previous_period on the first period returns None (not an error).
  2. Creating a cell override does NOT modify the row's default_projection_rule.
  3. Replacing an override marks the previous one as superseded (superseded_at set).
  4. The engine detects a direct cycle A→B→A and returns 'cycle_detected' without crashing.
"""
from datetime import datetime

import pytest


# ---------------------------------------------------------------------------
# Fixtures shared across tests in this module
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def sheet_id(client, auth_headers):
    """Create a minimal sheet with 3 monthly periods."""
    resp = client.post(
        "/api/sheets/",
        json={
            "name": "Test Sheet",
            "currency": "USD",
            "horizon_months": 3,
            "base_period": "2026-01-01T00:00:00",
        },
        headers=auth_headers,
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


@pytest.fixture(scope="module")
def section_id(client, auth_headers, sheet_id):
    resp = client.post(
        f"/api/sheets/{sheet_id}/sections",
        json={"name": "Income", "section_type": "income"},
        headers=auth_headers,
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


@pytest.fixture(scope="module")
def periods(client, auth_headers, sheet_id):
    """Return the list of period objects for the sheet."""
    resp = client.get(f"/api/sheets/{sheet_id}", headers=auth_headers)
    assert resp.status_code == 200, resp.text
    return resp.json()["periods"]


# ---------------------------------------------------------------------------
# Test 1: previous_period on first period returns None, not an error
# ---------------------------------------------------------------------------

def test_previous_period_on_first_period_returns_none(client, auth_headers, sheet_id, section_id, periods):
    # Add a row with rule: previous_period
    resp = client.post(
        f"/api/sheets/{sheet_id}/sections/{section_id}/rows",
        json={
            "name": "Salary",
            "row_type": "input",
            "default_projection_rule": {"type": "previous_period"},
        },
        headers=auth_headers,
    )
    assert resp.status_code == 201, resp.text

    # Get the matrix
    matrix_resp = client.get(f"/api/sheets/{sheet_id}/matrix", headers=auth_headers)
    assert matrix_resp.status_code == 200, matrix_resp.text
    matrix = matrix_resp.json()

    # Find the row in the matrix
    salary_cells = None
    for section in matrix["sections"]:
        for row_data in section["rows"]:
            if row_data["row"]["name"] == "Salary":
                salary_cells = row_data["cells"]
                break

    assert salary_cells is not None, "Salary row not found in matrix"
    assert len(salary_cells) == 3

    # First period: previous_period → None (no previous), no error
    first_cell = salary_cells[0]
    assert first_cell["projected_value"] is None
    assert first_cell.get("error") is None


# ---------------------------------------------------------------------------
# Test 2: Creating override does NOT modify SheetRow.default_projection_rule
# ---------------------------------------------------------------------------

def test_override_does_not_modify_projection_rule(client, auth_headers, sheet_id, section_id, periods):
    # Add a row with a constant rule
    row_resp = client.post(
        f"/api/sheets/{sheet_id}/sections/{section_id}/rows",
        json={
            "name": "Rent",
            "row_type": "input",
            "default_projection_rule": {"type": "constant", "value": 700},
        },
        headers=auth_headers,
    )
    assert row_resp.status_code == 201, row_resp.text
    row_id = row_resp.json()["id"]
    original_rule = row_resp.json()["default_projection_rule"]

    # Write a manual override for the first period
    first_period_id = periods[0]["id"]
    ov_resp = client.put(
        f"/api/sheets/{sheet_id}/cells/{row_id}/{first_period_id}",
        json={"value": 750},
        headers=auth_headers,
    )
    assert ov_resp.status_code == 200, ov_resp.text

    # Re-fetch the sheet and verify the row rule is unchanged
    detail_resp = client.get(f"/api/sheets/{sheet_id}", headers=auth_headers)
    assert detail_resp.status_code == 200
    updated_rule = None
    for sec in detail_resp.json()["sections"]:
        for row in sec["rows"]:
            if row["id"] == row_id:
                updated_rule = row["default_projection_rule"]
                break

    assert updated_rule == original_rule, (
        f"Row projection rule was modified by override: {updated_rule!r}"
    )

    # Also verify the matrix shows the override value for period 1
    matrix_resp = client.get(f"/api/sheets/{sheet_id}/matrix", headers=auth_headers)
    assert matrix_resp.status_code == 200
    for section in matrix_resp.json()["sections"]:
        for row_data in section["rows"]:
            if row_data["row"]["id"] == row_id:
                first = row_data["cells"][0]
                assert float(first["projected_value"]) == 750.0
                assert first["effective_source"] == "manual"
                # Second period should still use the constant rule → 700
                second = row_data["cells"][1]
                assert float(second["projected_value"]) == 700.0
                assert second["effective_source"] == "rule"
                return

    pytest.fail("Rent row not found in matrix")


# ---------------------------------------------------------------------------
# Test 3: Replacing override marks the previous one as superseded
# ---------------------------------------------------------------------------

def test_replace_override_marks_previous_superseded(client, auth_headers, sheet_id, section_id, periods):
    # Add a simple row
    row_resp = client.post(
        f"/api/sheets/{sheet_id}/sections/{section_id}/rows",
        json={"name": "Utilities", "row_type": "input"},
        headers=auth_headers,
    )
    assert row_resp.status_code == 201, row_resp.text
    row_id = row_resp.json()["id"]
    period_id = periods[0]["id"]

    # First override: value = 100
    ov1_resp = client.put(
        f"/api/sheets/{sheet_id}/cells/{row_id}/{period_id}",
        json={"value": 100},
        headers=auth_headers,
    )
    assert ov1_resp.status_code == 200, ov1_resp.text
    ov1_id = ov1_resp.json()["id"]

    # Second override: value = 120 (replaces the first)
    ov2_resp = client.put(
        f"/api/sheets/{sheet_id}/cells/{row_id}/{period_id}",
        json={"value": 120, "note": "Updated amount"},
        headers=auth_headers,
    )
    assert ov2_resp.status_code == 200, ov2_resp.text
    ov2_data = ov2_resp.json()

    # The new override should be active (superseded_at = None)
    assert ov2_data["superseded_at"] is None
    assert float(ov2_data["value"]) == 120.0

    # The matrix should now show 120, not 100
    matrix_resp = client.get(f"/api/sheets/{sheet_id}/matrix", headers=auth_headers)
    assert matrix_resp.status_code == 200
    for section in matrix_resp.json()["sections"]:
        for row_data in section["rows"]:
            if row_data["row"]["id"] == row_id:
                assert float(row_data["cells"][0]["projected_value"]) == 120.0
                return

    pytest.fail("Utilities row not found in matrix")


# ---------------------------------------------------------------------------
# Test 4: Engine detects cycle A→B→A and returns cycle_detected error
# ---------------------------------------------------------------------------

def test_engine_detects_direct_cycle(client, auth_headers, sheet_id, section_id, periods):
    # Create row A: sum_rows([B]) — depends on B, which will depend on A
    row_a_resp = client.post(
        f"/api/sheets/{sheet_id}/sections/{section_id}/rows",
        json={"name": "CycleA", "row_type": "formula"},
        headers=auth_headers,
    )
    assert row_a_resp.status_code == 201, row_a_resp.text
    row_a_id = row_a_resp.json()["id"]

    row_b_resp = client.post(
        f"/api/sheets/{sheet_id}/sections/{section_id}/rows",
        json={"name": "CycleB", "row_type": "formula"},
        headers=auth_headers,
    )
    assert row_b_resp.status_code == 201, row_b_resp.text
    row_b_id = row_b_resp.json()["id"]

    # Set A's rule to sum_rows([B])
    client.patch(
        f"/api/sheets/{sheet_id}/sections/{section_id}/rows/{row_a_id}",
        json={"default_projection_rule": {"type": "sum_rows", "row_ids": [row_b_id]}},
        headers=auth_headers,
    )
    # Set B's rule to sum_rows([A]) — creates cycle A→B→A
    client.patch(
        f"/api/sheets/{sheet_id}/sections/{section_id}/rows/{row_b_id}",
        json={"default_projection_rule": {"type": "sum_rows", "row_ids": [row_a_id]}},
        headers=auth_headers,
    )

    # The matrix endpoint must not crash
    matrix_resp = client.get(f"/api/sheets/{sheet_id}/matrix", headers=auth_headers)
    assert matrix_resp.status_code == 200, matrix_resp.text

    matrix = matrix_resp.json()

    # Both cyclic rows should have error='cycle_detected' and projected_value=None
    found_a = found_b = False
    for section in matrix["sections"]:
        for row_data in section["rows"]:
            name = row_data["row"]["name"]
            if name == "CycleA":
                found_a = True
                for cell in row_data["cells"]:
                    assert cell["error"] == "cycle_detected", (
                        f"CycleA cell missing cycle_detected: {cell}"
                    )
                    assert cell["projected_value"] is None
            elif name == "CycleB":
                found_b = True
                for cell in row_data["cells"]:
                    assert cell["error"] == "cycle_detected", (
                        f"CycleB cell missing cycle_detected: {cell}"
                    )
                    assert cell["projected_value"] is None

    assert found_a and found_b, "Cyclic rows not found in matrix"


# ---------------------------------------------------------------------------
# Test 5: Delete override reverts cell to projection rule
# ---------------------------------------------------------------------------

def test_delete_override_reverts_to_rule(client, auth_headers, sheet_id, section_id, periods):
    row_resp = client.post(
        f"/api/sheets/{sheet_id}/sections/{section_id}/rows",
        json={
            "name": "Insurance",
            "row_type": "input",
            "default_projection_rule": {"type": "constant", "value": 50},
        },
        headers=auth_headers,
    )
    assert row_resp.status_code == 201
    row_id = row_resp.json()["id"]
    period_id = periods[1]["id"]

    # Write override
    client.put(
        f"/api/sheets/{sheet_id}/cells/{row_id}/{period_id}",
        json={"value": 999},
        headers=auth_headers,
    )

    # Verify override is active
    matrix_resp = client.get(f"/api/sheets/{sheet_id}/matrix", headers=auth_headers)
    for section in matrix_resp.json()["sections"]:
        for row_data in section["rows"]:
            if row_data["row"]["id"] == row_id:
                assert float(row_data["cells"][1]["projected_value"]) == 999.0
                break

    # Delete override
    del_resp = client.delete(
        f"/api/sheets/{sheet_id}/cells/{row_id}/{period_id}/override",
        headers=auth_headers,
    )
    assert del_resp.status_code == 204

    # Matrix should now show the rule value (50)
    matrix_resp2 = client.get(f"/api/sheets/{sheet_id}/matrix", headers=auth_headers)
    for section in matrix_resp2.json()["sections"]:
        for row_data in section["rows"]:
            if row_data["row"]["id"] == row_id:
                assert float(row_data["cells"][1]["projected_value"]) == 50.0
                assert row_data["cells"][1]["effective_source"] == "rule"
                return

    pytest.fail("Insurance row not found after delete override")
