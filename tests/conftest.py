import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from backend.database import Base, get_db
from backend.main import app

TEST_DB_URL = "sqlite:///:memory:"
engine = create_engine(TEST_DB_URL, connect_args={"check_same_thread": False})

# Share a single in-memory connection across all sessions so the DB persists
_connection = engine.connect()


def override_get_db():
    db = sessionmaker(autocommit=False, autoflush=False, bind=_connection)()
    try:
        yield db
    finally:
        db.close()


@pytest.fixture(scope="module")
def client():
    Base.metadata.create_all(bind=_connection)
    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as c:
        yield c
    Base.metadata.drop_all(bind=_connection)
    app.dependency_overrides.clear()


@pytest.fixture(scope="module")
def auth_headers(client):
    client.post(
        "/api/auth/register",
        json={"username": "testuser", "email": "test@example.com", "password": "pass1234"},
    )
    resp = client.post(
        "/api/auth/token",
        data={"username": "testuser", "password": "pass1234"},
    )
    assert resp.status_code == 200
    token = resp.json()["access_token"]
    return {"Authorization": f"Bearer {token}"}
