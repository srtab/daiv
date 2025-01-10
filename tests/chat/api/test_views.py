import pytest
from ninja.testing import TestClient

from accounts.models import APIKey, User
from chat.api.security import AuthBearer
from chat.api.views import MODEL_ID
from core.constants import BOT_NAME
from daiv.api import api


@pytest.fixture
def client():
    user = User.objects.create_user(username="testuser", email="test@example.com", password="testpass123")  # noqa: S106
    api_key = APIKey.objects.create_key(user=user, name="Test Key")[1]
    return TestClient(api, headers={AuthBearer.header: f"Bearer {api_key}"})


@pytest.fixture
def client_unauthenticated():
    return TestClient(api)


@pytest.mark.django_db
def test_get_models_unauthenticated(client_unauthenticated: TestClient):
    response = client_unauthenticated.get("/chat/models")
    assert response.status_code == 401


@pytest.mark.django_db
def test_get_models(client: TestClient):
    response = client.get("/chat/models")
    assert response.status_code == 200
    assert response.json() == {
        "object": "list",
        "data": [{"id": MODEL_ID, "object": "model", "created": None, "owned_by": BOT_NAME}],
    }


@pytest.mark.django_db
def test_get_model_detail_unauthenticated(client_unauthenticated: TestClient):
    response = client_unauthenticated.get(f"/chat/models/{MODEL_ID}")
    assert response.status_code == 401


@pytest.mark.django_db
def test_get_model_detail_valid_id(client: TestClient):
    response = client.get(f"/chat/models/{MODEL_ID}")
    assert response.status_code == 200
    assert response.json() == {"id": MODEL_ID, "object": "model", "created": None, "owned_by": BOT_NAME}


@pytest.mark.django_db
def test_get_model_detail_invalid_id(client: TestClient):
    response = client.get("/chat/models/invalid_model")
    assert response.status_code == 404
