from django.http import HttpRequest  # noqa: TC002 - required at runtime by Django Ninja

from asgiref.sync import async_to_sync
from ninja import Router
from ninja.security import django_auth

from automation.api.schemas import AgentModelsResponse, ProviderInfo
from automation.picker.context import enabled_providers, provider_label
from automation.picker.model_catalog.service import fetch_catalog

router = Router(tags=["automation"])


@router.get("/agent/models", response=AgentModelsResponse, auth=django_auth, url_name="agent_models")
def agent_models(request: HttpRequest) -> AgentModelsResponse:
    """Return the model catalog for the agent picker.

    Disabled providers are filtered out. HTTP 200 even when individual
    providers errored — the per-provider error lives in the payload.
    """
    enabled_rows = enabled_providers()
    catalog = async_to_sync(fetch_catalog)(enabled_rows)

    return AgentModelsResponse(
        providers=[ProviderInfo(slug=row.slug, label=provider_label(row)) for row in enabled_rows], catalog=catalog
    )
