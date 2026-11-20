import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.store import ItemStore, store


@pytest.fixture(autouse=True)
def fresh_store():
    store.__dict__.update(ItemStore().__dict__)


@pytest.fixture
def client():
    return TestClient(app)
