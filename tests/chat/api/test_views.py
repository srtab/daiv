from django.http import Http404
from django.test import RequestFactory

import pytest
from chat.api.views import MODEL_ID, get_model, get_models

from core.constants import BOT_NAME


@pytest.fixture
def request_factory():
    return RequestFactory()


def test_get_models(request_factory):
    request = request_factory.get("/api/v1/models")
    response = get_models(request)

    assert response.object == "list"
    assert len(response.data) == 1

    model = response.data[0]
    assert model.id == MODEL_ID
    assert model.object == "model"
    assert model.owned_by == BOT_NAME
    assert model.created is None


def test_get_model_valid_id(request_factory):
    request = request_factory.get(f"/api/v1/models/{MODEL_ID}")
    response = get_model(request, MODEL_ID)

    assert response.id == MODEL_ID
    assert response.object == "model"
    assert response.owned_by == BOT_NAME
    assert response.created is None


def test_get_model_invalid_id(request_factory):
    request = request_factory.get("/api/v1/models/invalid_model")

    with pytest.raises(Http404):
        get_model(request, "invalid_model")
