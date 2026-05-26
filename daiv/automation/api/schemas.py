from ninja import Schema


class ProviderInfo(Schema):
    slug: str
    label: str


class ProviderCatalogEntry(Schema):
    models: list[str]
    error: str | None


class AgentModelsResponse(Schema):
    providers: list[ProviderInfo]
    catalog: dict[str, ProviderCatalogEntry]
