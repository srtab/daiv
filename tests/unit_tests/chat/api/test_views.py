import pytest
from ninja.testing import TestAsyncClient

from chat.api.views import MODEL_ID
from daiv.api import api


@pytest.fixture
def client_unauthenticated():
    return TestAsyncClient(api)


@pytest.mark.django_db
async def test_create_chat_completion(client_unauthenticated: TestAsyncClient):
    response = await client_unauthenticated.post(
        "/chat/completions", json={"model": MODEL_ID, "messages": [{"role": "user", "content": "Hello, how are you?"}]}
    )
    assert response.status_code == 401


@pytest.mark.django_db
async def test_get_models_unauthenticated(client_unauthenticated: TestAsyncClient):
    response = await client_unauthenticated.get("/models")
    assert response.status_code == 401


@pytest.mark.django_db
async def test_get_model_detail_unauthenticated(client_unauthenticated: TestAsyncClient):
    response = await client_unauthenticated.get(f"/models/{MODEL_ID}")
    assert response.status_code == 401
