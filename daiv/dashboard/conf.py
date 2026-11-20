from uuid import UUID  # noqa: TC003 - required at runtime by Pydantic

from pydantic import Field, HttpUrl
from pydantic_settings import BaseSettings, SettingsConfigDict


class DashboardSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="DAIV_", env_parse_none_str="None")

    UMAMI_WEBSITE_ID: UUID | None = Field(
        default=None,
        description=(
            "Umami website ID. When set, the public homepage and comparison pages load Umami's cookieless "
            "tracker for signed-out visitors. Unset, no page loads it."
        ),
    )
    UMAMI_SCRIPT_URL: HttpUrl = Field(
        default=HttpUrl("https://cloud.umami.is/script.js"),
        description="URL of Umami's tracker script; point it at a self-hosted Umami instead of Umami Cloud.",
    )


settings = DashboardSettings()
